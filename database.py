"""
database.py — Capa de acceso a datos para rAÍz

Detecta el backend automáticamente:
  - Supabase  si SUPABASE_URL está en .streamlit/secrets.toml
  - SQLite    (raiz_local.db) como fallback offline

Ambos backends exponen la misma API pública, por lo que app.py
y auth.py son completamente agnósticos al backend activo.
"""

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional, Tuple
import hashlib
import base64
from cryptography.fernet import Fernet
import streamlit as st

def _get_fernet() -> Fernet:
    """Retorna la instancia de Fernet usando la clave simétrica de los secretos."""
    try:
        # Intentar obtener la clave de secrets.toml
        key = st.secrets["CELULAR_ENCRYPTION_KEY"]
    except (FileNotFoundError, KeyError):
        # Fallback de desarrollo temporal (IMPORTANTE: configurar en prod)
        key = base64.urlsafe_b64encode(b"01234567890123456789012345678901").decode()
    return Fernet(key)

def encriptar_celular(celular: Optional[str]) -> Optional[str]:
    """Encripta el número de celular usando AES-128."""
    if not celular: return None
    return _get_fernet().encrypt(celular.encode("utf-8")).decode("utf-8")

def desencriptar_celular(celular_enc: Optional[str]) -> Optional[str]:
    """Desencripta el número de celular."""
    if not celular_enc: return None
    try:
        return _get_fernet().decrypt(celular_enc.encode("utf-8")).decode("utf-8")
    except Exception:
        # Fallback si el celular está en texto plano en la DB (ej. previo a la migración)
        return celular_enc


# ── Detección de backend ───────────────────────────────────────────────────────

def _use_supabase() -> bool:
    try:
        return bool(st.secrets.get("SUPABASE_URL", ""))
    except Exception:
        return False


# ── Cliente Supabase (singleton cacheado) ──────────────────────────────────────

@st.cache_resource
def _get_supabase():
    from supabase import create_client
    return create_client(
        st.secrets["SUPABASE_URL"],
        st.secrets["SUPABASE_SERVICE_KEY"],
    )


# ── SQLite: DDL ────────────────────────────────────────────────────────────────
# Esquema equivalente al de schema.sql, adaptado a tipos nativos de SQLite.
# Los tipos TEXT/INTEGER/REAL son los únicos que SQLite reconoce internamente.

SQLITE_PATH = "raiz_local.db"

_DDL = """
CREATE TABLE IF NOT EXISTS municipios (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo TEXT    UNIQUE NOT NULL,
    nombre TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS instituciones (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    municipio_id        INTEGER NOT NULL REFERENCES municipios(id),
    nombre              TEXT    NOT NULL,
    orientador_nombre   TEXT,
    orientador_email    TEXT,
    orientador_telefono TEXT,
    rector_nombre       TEXT,
    rector_email        TEXT
);
CREATE TABLE IF NOT EXISTS sedes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    institucion_id    INTEGER NOT NULL REFERENCES instituciones(id),
    nombre            TEXT    NOT NULL,
    es_sede_principal INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admins_sede (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sede_id        INTEGER NOT NULL REFERENCES sedes(id),
    email_rector   TEXT,
    codigo_admin   TEXT UNIQUE NOT NULL,
    fecha_registro TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS administradores (
    id                 TEXT    PRIMARY KEY,
    nombre             TEXT    NOT NULL,
    email              TEXT    UNIQUE NOT NULL,
    rol                TEXT    NOT NULL CHECK (rol IN ('fcc', 'orientador', 'secretaria', 'rector')),
    institucion_id     INTEGER REFERENCES instituciones(id),
    municipio_id       INTEGER REFERENCES municipios(id),
    activo             INTEGER DEFAULT 1,
    fecha_creacion     TEXT    DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS estudiantes (
    id                                  TEXT    PRIMARY KEY,
    estudiante_id                       TEXT    UNIQUE NOT NULL,
    nombre                              TEXT    NOT NULL,
    apellido                            TEXT    NOT NULL,
    grado                               INTEGER NOT NULL,
    email                               TEXT    UNIQUE,
    celular_hash                        TEXT,
    sede_id                             INTEGER NOT NULL REFERENCES sedes(id),
    asentimiento_estudiante             INTEGER DEFAULT 0,
    fecha_asentimiento_estudiante       TEXT,
    consentimiento_acudiente_verificado INTEGER DEFAULT 0,
    fecha_verificacion_acudiente        TEXT,
    administrador_registro_id           TEXT    REFERENCES administradores(id),
    consentimiento_datos_sensibles      INTEGER DEFAULT 0,
    fecha_consentimiento_sensibles      TEXT,
    fecha_registro                      TEXT    DEFAULT (datetime('now')),
    sesion_actual                       INTEGER DEFAULT 1,
    momento_actual                      INTEGER DEFAULT 1,
    perfil_riesgo                       TEXT    DEFAULT 'sin_evaluar',
    mentoria_completada                 INTEGER DEFAULT 0,
    consentimiento_archivo_url          TEXT,
    suprimido                           INTEGER DEFAULT 0,
    fecha_supresion                     TEXT,
    motivo_supresion                    TEXT,
    fecha_retencion_hasta               TEXT,
    celular                             TEXT,
    CHECK (email IS NOT NULL OR celular_hash IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS mensajes (
    id            TEXT    PRIMARY KEY,
    estudiante_id TEXT    NOT NULL REFERENCES estudiantes(id),
    sesion_numero INTEGER NOT NULL,
    rol           TEXT    NOT NULL,
    contenido     TEXT    NOT NULL,
    timestamp     TEXT    DEFAULT (datetime('now')),
    tiene_alerta  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alertas (
    id                     TEXT    PRIMARY KEY,
    estudiante_id          TEXT    NOT NULL REFERENCES estudiantes(id),
    sede_id                INTEGER NOT NULL REFERENCES sedes(id),
    tipo                   TEXT    NOT NULL,
    estado                 TEXT    DEFAULT 'pendiente',
    timestamp              TEXT    DEFAULT (datetime('now')),
    nota_resolucion        TEXT,
    notificado_orientador  INTEGER DEFAULT 0,
    notificado_rector      INTEGER DEFAULT 0,
    notificado_peas        INTEGER DEFAULT 0,
    timestamp_notificacion TEXT
);
CREATE TABLE IF NOT EXISTS whatsapp_mensajes (
    id              TEXT    PRIMARY KEY,
    estudiante_id   TEXT    NOT NULL REFERENCES estudiantes(id),
    mensaje_numero  INTEGER NOT NULL CHECK (mensaje_numero BETWEEN 0 AND 5),
    enviado_at      TEXT    DEFAULT (datetime('now')),
    estado          TEXT    DEFAULT 'enviado'
);
CREATE TABLE IF NOT EXISTS envios_ficha (
    id              TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    estudiante_id   TEXT    NOT NULL,
    orientador_email TEXT   NOT NULL,
    exito           INTEGER NOT NULL,
    timestamp       TEXT    DEFAULT (datetime('now'))
);
"""

# ── SQLite: datos seed ─────────────────────────────────────────────────────────
# Idénticos a los de schema.sql. Instituciones y sedes reales del piloto.

_SEED_MUNICIPIOS = [
    ("ALC", "Alcalá"),        ("ANS", "Ansermanuevo"), ("CAN", "Candelaria"),
    ("CER", "Cerrito"),       ("CAI", "El Cairo"),     ("AGU", "El Águila"),
    ("GUA", "Guacarí"),       ("OBA", "Obando"),       ("SPE", "San Pedro"),
    ("TOR", "Toro"),          ("VIJ", "Vijes"),
]

_SEED_INSTITUCIONES = [
    # Alcalá
    ("ALC", "IE Arturo Gómez Jaramillo", [
        ("Principal",            True),
        ("José Ignacio Rengifo", False),
    ]),
    ("ALC", "IE San José", [("Principal", True)]),
    # Ansermanuevo
    ("ANS", "IE El Placer",                    [("Principal", True)]),
    ("ANS", "IE Jorge Isaacs",                 [("Principal", True)]),
    ("ANS", "IE Santa Ana De Los Caballeros",  [("Principal", True)]),
    ("ANS", "IE Santa Inés",                   [("Principal", True)]),
    # Candelaria
    ("CAN", "Nuestra Señora De La Candelaria", [("Principal", True)]),
    # Cerrito
    ("CER", "IE Jorge Isaacs - El Placer",     [("Principal", True)]),
    # El Águila
    ("AGU", "IE El Águila",                    [("Principal", True)]),
    ("AGU", "IE Justiniano Echavarría", [
        ("Principal",        True),
        ("Sede Santa Isabel", False),
    ]),
    ("AGU", "IE Santa Marta", [
        ("Principal",      True),
        ("Dionisio Cortez", False),
    ]),
    # El Cairo
    ("CAI", "IE Gilberto Alzate Avendaño", [
        ("Principal", True),
        ("Alban",     False),
    ]),
    ("CAI", "IE La Presentación",              [("Principal", True)]),
    # Guacarí
    ("GUA", "IE Normal Superior Miguel De Cervantes Saavedra", [("Principal", True)]),
    # Obando
    ("OBA", "IE María Analía Ortiz Hormaza",   [("Principal", True)]),
    ("OBA", "IE Policarpa Salavarrieta",        [("Principal", True)]),
    ("OBA", "IE San José",                     [("Principal", True)]),
    # San Pedro
    ("SPE", "IE José Antonio Aguilera",        [("Principal", True)]),
    ("SPE", "IE Julio Caicedo Téllez",         [("Principal", True)]),
    ("SPE", "IE Miguel Antonio Caro", [
        ("Principal",       True),
        ("Gabriela Mistral", False),
    ]),
    # Toro
    ("TOR", "IE Fray José Joaquín Escobar",        [("Principal", True)]),
    ("TOR", "IE Nuestra Señora De La Consolación", [("Principal", True)]),
    ("TOR", "IE Técnica Agropecuaria Toro", [
        ("Principal",                True),
        ("Nuestra Señora de Fátima", False),
    ]),
    # Vijes
    ("VIJ", "IE Antonio José De Sucre", [
        ("Principal",              True),
        ("Sede Atanasio Girardot", False),
    ]),
    ("VIJ", "IE Jorge Robledo",                [("Principal", True)]),
    ("VIJ", "IE 20 De Julio", [
        ("Principal",           True),
        ("Sede Manuela Beltrán", False),
    ]),
]

_sqlite_ready = False


# ── SQLite: helpers de migración ───────────────────────────────────────────────

def _cols(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_col(conn, table: str, col: str, defn: str) -> None:
    if col not in _cols(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")


def _rename_col(conn, table: str, old: str, new: str) -> None:
    c = _cols(conn, table)
    if old in c and new not in c:
        conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")


# ── SQLite: helpers internos ───────────────────────────────────────────────────

@contextmanager
def _conn():
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_sqlite():
    global _sqlite_ready
    if _sqlite_ready:
        return
    with _conn() as conn:
        conn.executescript(_DDL)
        if conn.execute("SELECT COUNT(*) FROM municipios").fetchone()[0] == 0:
            conn.executemany(
                "INSERT OR IGNORE INTO municipios (codigo, nombre) VALUES (?, ?)",
                _SEED_MUNICIPIOS,
            )
            for mun_codigo, inst_nombre, sedes in _SEED_INSTITUCIONES:
                mun_id = conn.execute(
                    "SELECT id FROM municipios WHERE codigo = ?", (mun_codigo,)
                ).fetchone()[0]
                cur = conn.execute(
                    "INSERT INTO instituciones (municipio_id, nombre) VALUES (?, ?)",
                    (mun_id, inst_nombre),
                )
                conn.executemany(
                    "INSERT INTO sedes (institucion_id, nombre, es_sede_principal) VALUES (?, ?, ?)",
                    [(cur.lastrowid, nombre, int(principal)) for nombre, principal in sedes],
                )

        # ── Migración 002: roles y consentimiento (idempotente) ────────────────
        _rename_col(conn, "estudiantes", "consentimiento_habeas_data",  "asentimiento_estudiante")
        _rename_col(conn, "estudiantes", "fecha_consentimiento",        "fecha_asentimiento_estudiante")
        _add_col(conn, "estudiantes", "consentimiento_acudiente_verificado", "INTEGER DEFAULT 0")
        _add_col(conn, "estudiantes", "fecha_verificacion_acudiente",        "TEXT")
        _add_col(conn, "estudiantes", "administrador_registro_id",           "TEXT")
        _add_col(conn, "estudiantes", "consentimiento_datos_sensibles",      "INTEGER DEFAULT 0")
        _add_col(conn, "estudiantes", "fecha_consentimiento_sensibles",      "TEXT")
        _add_col(conn, "instituciones", "rector_nombre", "TEXT")
        _add_col(conn, "instituciones", "rector_email",  "TEXT")
        _add_col(conn, "alertas", "notificado_orientador",   "INTEGER DEFAULT 0")
        _add_col(conn, "alertas", "notificado_rector",       "INTEGER DEFAULT 0")
        _add_col(conn, "alertas", "notificado_peas",         "INTEGER DEFAULT 0")
        _add_col(conn, "alertas", "timestamp_notificacion",  "TEXT")
        _add_col(conn, "administradores", "municipio_id",    "INTEGER")
        # Ajuste piloto: celular_hash opcional (al menos email o celular requerido)
        # Nota: DROP NOT NULL sobre email no es soportado en SQLite via ALTER TABLE.
        # El _DDL ya lo refleja NULLable para instalaciones frescas. En DBs existentes
        # la restricción la aplica la capa de aplicación (formulario del administrador).
        _add_col(conn, "estudiantes", "celular_hash",              "TEXT")
        _add_col(conn, "estudiantes", "consentimiento_archivo_url", "TEXT")

        # ── Migración 004: supresión y retención (idempotente) ─────────────────
        _add_col(conn, "estudiantes", "suprimido",             "INTEGER DEFAULT 0")
        _add_col(conn, "estudiantes", "fecha_supresion",       "TEXT")
        _add_col(conn, "estudiantes", "motivo_supresion",      "TEXT")
        _add_col(conn, "estudiantes", "fecha_retencion_hasta", "TEXT")

        # ── Migración 005: WhatsApp re-engagement (idempotente) ─────────────────
        _add_col(conn, "estudiantes", "celular", "TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS whatsapp_mensajes (
                id              TEXT    PRIMARY KEY,
                estudiante_id   TEXT    NOT NULL REFERENCES estudiantes(id),
                mensaje_numero  INTEGER NOT NULL CHECK (mensaje_numero BETWEEN 0 AND 5),
                enviado_at      TEXT    DEFAULT (datetime('now')),
                estado          TEXT    DEFAULT 'enviado'
            )
            """
        )

        # ── Migración 006: envíos de ficha al orientador (idempotente) ───────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS envios_ficha (
                id              TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
                estudiante_id   TEXT    NOT NULL,
                orientador_email TEXT   NOT NULL,
                exito           INTEGER NOT NULL,
                timestamp       TEXT    DEFAULT (datetime('now'))
            )
            """
        )
    _sqlite_ready = True


def _generar_id_sqlite(conn, sede_id: int, grado: int) -> str:
    """Equivalente Python de la función generar_estudiante_id() de PostgreSQL."""
    year = datetime.now().year
    row = conn.execute(
        """
        SELECT m.codigo, m.id AS municipio_id
        FROM   sedes        s
        JOIN   instituciones i ON s.institucion_id = i.id
        JOIN   municipios    m ON i.municipio_id   = m.id
        WHERE  s.id = ?
        """,
        (sede_id,),
    ).fetchone()

    count = conn.execute(
        """
        SELECT COUNT(*)
        FROM   estudiantes   e
        JOIN   sedes         s  ON e.sede_id         = s.id
        JOIN   instituciones i  ON s.institucion_id  = i.id
        WHERE  i.municipio_id = ?
          AND  e.grado        = ?
          AND  strftime('%Y', e.fecha_registro) = ?
        """,
        (row["municipio_id"], grado, str(year)),
    ).fetchone()[0]

    return f"{row['codigo']}-{grado}-{year}-{count + 1:04d}"


# ── API pública: dropdowns para el formulario de registro ─────────────────────

def get_municipios() -> list[dict]:
    if _use_supabase():
        r = _get_supabase().table("municipios").select("id, codigo, nombre").order("nombre").execute()
        return r.data
    _ensure_sqlite()
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, codigo, nombre FROM municipios ORDER BY nombre"
        ).fetchall()]


def get_instituciones(municipio_id: int) -> list[dict]:
    if _use_supabase():
        r = (
            _get_supabase().table("instituciones")
            .select("id, nombre")
            .eq("municipio_id", municipio_id)
            .order("nombre")
            .execute()
        )
        return r.data
    _ensure_sqlite()
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT id, nombre FROM instituciones WHERE municipio_id = ? ORDER BY nombre",
            (municipio_id,),
        ).fetchall()]


def get_sedes(institucion_id: int) -> list[dict]:
    if _use_supabase():
        r = (
            _get_supabase().table("sedes")
            .select("id, nombre, es_sede_principal")
            .eq("institucion_id", institucion_id)
            .order("es_sede_principal", desc=True)
            .execute()
        )
        return r.data
    _ensure_sqlite()
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT id, nombre, es_sede_principal
            FROM   sedes
            WHERE  institucion_id = ?
            ORDER  BY es_sede_principal DESC, nombre
            """,
            (institucion_id,),
        ).fetchall()]


# ── API pública: ciclo de vida del estudiante ──────────────────────────────────

def crear_estudiante(
    nombre: str,
    apellido: str,
    grado: int,
    email: str,
    sede_id: int,
) -> Optional[str]:
    """
    Crea un nuevo estudiante y retorna su estudiante_id generado (ej. 'ALC-9-2026-0042').
    Retorna None si el email ya está registrado.
    El consentimiento habeas data se registra por separado con set_consentimiento().
    """
    email = email.lower().strip()
    new_uuid = str(uuid.uuid4())

    if _use_supabase():
        sb = _get_supabase()
        est_id = sb.rpc(
            "generar_estudiante_id", {"p_sede_id": sede_id, "p_grado": grado}
        ).execute().data
        try:
            sb.table("estudiantes").insert({
                "id":               new_uuid,
                "estudiante_id":    est_id,
                "nombre":           nombre,
                "apellido":         apellido,
                "grado":            grado,
                "email":            email,
                "sede_id":          sede_id,
            }).execute()
        except Exception as e:
            if "duplicate" in str(e).lower() or "unique" in str(e).lower():
                return None
            raise
        return est_id

    _ensure_sqlite()
    with _conn() as conn:
        if conn.execute(
            "SELECT id FROM estudiantes WHERE email = ?", (email,)
        ).fetchone():
            return None
        est_id = _generar_id_sqlite(conn, sede_id, grado)
        conn.execute(
            """
            INSERT INTO estudiantes
                (id, estudiante_id, nombre, apellido, grado, email, sede_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_uuid, est_id, nombre, apellido, grado, email, sede_id),
        )
    return est_id


def login_estudiante(estudiante_id: str) -> Optional[dict]:
    """Retorna el dict completo del estudiante o None si el ID no existe."""
    eid = estudiante_id.strip().upper()

    if _use_supabase():
        r = (
            _get_supabase().table("estudiantes")
            .select("*")
            .eq("estudiante_id", eid)
            .limit(1)
            .execute()
        )
        if not r.data:
            return None
        est = r.data[0]
        est["celular"] = desencriptar_celular(est.get("celular"))
        return est

    _ensure_sqlite()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM estudiantes WHERE estudiante_id = ?", (eid,)
        ).fetchone()
        if not row:
            return None
        est = dict(row)
        est["celular"] = desencriptar_celular(est.get("celular"))
        return est


def get_estudiante_por_email(email: str) -> Optional[dict]:
    """
    Flujo 'Olvidé mi ID': busca por email y retorna estudiante_id + nombre.
    Retorna None si el email no está registrado.
    """
    email = email.lower().strip()

    if _use_supabase():
        r = (
            _get_supabase().table("estudiantes")
            .select("estudiante_id, nombre, email")
            .eq("email", email)
            .limit(1)
            .execute()
        )
        return r.data[0] if r.data else None

    _ensure_sqlite()
    with _conn() as conn:
        row = conn.execute(
            "SELECT estudiante_id, nombre, email FROM estudiantes WHERE email = ?",
            (email,),
        ).fetchone()
        return dict(row) if row else None


def set_consentimiento(
    estudiante_uuid: str,
    incluye_datos_sensibles: bool = True,
) -> None:
    """
    Graba el asentimiento informado del estudiante con timestamp preciso (Ley 1581/2012).
    incluye_datos_sensibles=True cuando el estudiante acepta también el checkbox de
    datos sensibles (salud, situación socioeconómica) — ver PENDIENTE 7 del backlog.
    """
    now = datetime.now(timezone.utc).isoformat()
    payload: dict = {
        "asentimiento_estudiante":           True,
        "fecha_asentimiento_estudiante":     now,
        "consentimiento_datos_sensibles":    incluye_datos_sensibles,
    }
    if incluye_datos_sensibles:
        payload["fecha_consentimiento_sensibles"] = now

    if _use_supabase():
        _get_supabase().table("estudiantes").update(payload).eq("id", estudiante_uuid).execute()
        return

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE estudiantes
            SET    asentimiento_estudiante          = 1,
                   fecha_asentimiento_estudiante    = ?,
                   consentimiento_datos_sensibles   = ?,
                   fecha_consentimiento_sensibles   = ?
            WHERE  id = ?
            """,
            (now, int(incluye_datos_sensibles), now if incluye_datos_sensibles else None, estudiante_uuid),
        )


def update_progreso(estudiante_uuid: str, sesion: int, momento: int) -> None:
    """Actualiza el puntero de avance pedagógico para permitir retomar la sesión."""
    if _use_supabase():
        _get_supabase().table("estudiantes").update({
            "sesion_actual":  sesion,
            "momento_actual": momento,
        }).eq("id", estudiante_uuid).execute()
        return

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            "UPDATE estudiantes SET sesion_actual = ?, momento_actual = ? WHERE id = ?",
            (sesion, momento, estudiante_uuid),
        )


def update_perfil_riesgo(estudiante_uuid: str, perfil: str) -> None:
    """perfil: 'bajo' | 'medio' | 'alto' — inferido del motor de riesgo del prompt."""
    if _use_supabase():
        _get_supabase().table("estudiantes").update(
            {"perfil_riesgo": perfil}
        ).eq("id", estudiante_uuid).execute()
        return

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            "UPDATE estudiantes SET perfil_riesgo = ? WHERE id = ?",
            (perfil, estudiante_uuid),
        )


def set_mentoria_completada(estudiante_uuid: str) -> None:
    """Marca la mentoría como completada cuando el modelo emite [FIN_CONSEJERIA]."""
    if _use_supabase():
        _get_supabase().table("estudiantes").update(
            {"mentoria_completada": True}
        ).eq("id", estudiante_uuid).execute()
        return

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            "UPDATE estudiantes SET mentoria_completada = 1 WHERE id = ?",
            (estudiante_uuid,),
        )


# ── API pública: historial de chat ─────────────────────────────────────────────

def guardar_mensaje(
    estudiante_uuid: str,
    sesion: int,
    rol: str,
    contenido: str,
    tiene_alerta: bool = False,
) -> None:
    """
    Guarda el mensaje con su contenido RAW (con etiquetas internas como [RIESGO_ALTO]).
    No llamar a limpiar_etiquetas() aquí — el SDK de Gemini necesita el historial
    completo con etiquetas para reconstruir coherentemente la sesión al reingresar.
    """
    msg_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    if _use_supabase():
        _get_supabase().table("mensajes").insert({
            "id":            msg_id,
            "estudiante_id": estudiante_uuid,
            "sesion_numero": sesion,
            "rol":           rol,
            "contenido":     contenido,
            "timestamp":     now,
            "tiene_alerta":  tiene_alerta,
        }).execute()
        return

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO mensajes
                (id, estudiante_id, sesion_numero, rol, contenido, timestamp, tiene_alerta)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (msg_id, estudiante_uuid, sesion, rol, contenido, now, int(tiene_alerta)),
        )


def get_historial(
    estudiante_uuid: str,
    sesion: Optional[int] = None,
) -> list[dict]:
    """
    Retorna los mensajes ordenados por timestamp para reconstruir el historial en Gemini.
    sesion=None  → todas las sesiones (para reconstruir todo el contexto)
    sesion=N     → solo esa sesión
    """
    if _use_supabase():
        sb = _get_supabase()
        q = (
            sb.table("mensajes")
            .select("rol, contenido, sesion_numero, timestamp")
            .eq("estudiante_id", estudiante_uuid)
            .order("timestamp")
        )
        if sesion is not None:
            q = q.eq("sesion_numero", sesion)
        return q.execute().data

    _ensure_sqlite()
    with _conn() as conn:
        if sesion is not None:
            rows = conn.execute(
                """
                SELECT rol, contenido, sesion_numero, timestamp
                FROM   mensajes
                WHERE  estudiante_id = ? AND sesion_numero = ?
                ORDER  BY timestamp
                """,
                (estudiante_uuid, sesion),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT rol, contenido, sesion_numero, timestamp
                FROM   mensajes
                WHERE  estudiante_id = ?
                ORDER  BY timestamp
                """,
                (estudiante_uuid,),
            ).fetchall()
        return [dict(r) for r in rows]


# ── API pública: alertas ───────────────────────────────────────────────────────

def crear_alerta(estudiante_uuid: str, sede_id: int, tipo: str) -> str:
    """
    Crea una alerta y retorna su UUID.
    tipo: 'orientador_requerida' | 'psicologica_critica'
    El UUID es necesario para llamar a update_notificaciones_alerta() después del envío.
    """
    alert_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    if _use_supabase():
        _get_supabase().table("alertas").insert({
            "id":            alert_id,
            "estudiante_id": estudiante_uuid,
            "sede_id":       sede_id,
            "tipo":          tipo,
            "timestamp":     now,
        }).execute()
        return alert_id

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO alertas (id, estudiante_id, sede_id, tipo, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            (alert_id, estudiante_uuid, sede_id, tipo, now),
        )
    return alert_id


def get_sede_info(sede_id: int) -> dict:
    """
    Retorna {institucion, municipio, orientador_nombre, orientador_email,
    orientador_telefono, rector_email} para una sede.
    Usado por pdf_generator y email_service para encabezados y notificaciones.
    """
    if _use_supabase():
        r = (
            _get_supabase()
            .table("sedes")
            .select(
                "nombre, instituciones("
                "nombre, orientador_nombre, orientador_email, orientador_telefono, "
                "rector_email, municipios(nombre))"
            )
            .eq("id", sede_id)
            .limit(1)
            .execute()
        )
        if not r.data:
            return {
                "institucion": "", "municipio": "",
                "orientador_nombre": "", "orientador_email": "",
                "orientador_telefono": "", "rector_email": "",
            }
        row = r.data[0]
        inst = row.get("instituciones") or {}
        mun = inst.get("municipios") or {}
        return {
            "institucion":         inst.get("nombre", ""),
            "municipio":           mun.get("nombre", ""),
            "orientador_nombre":   inst.get("orientador_nombre", ""),
            "orientador_email":    inst.get("orientador_email", ""),
            "orientador_telefono": inst.get("orientador_telefono", ""),
            "rector_email":        inst.get("rector_email", ""),
        }

    _ensure_sqlite()
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT i.nombre             AS institucion,
                   m.nombre             AS municipio,
                   i.orientador_nombre,
                   i.orientador_email,
                   i.orientador_telefono,
                   i.rector_email
            FROM   sedes         s
            JOIN   instituciones i ON s.institucion_id = i.id
            JOIN   municipios    m ON i.municipio_id   = m.id
            WHERE  s.id = ?
            """,
            (sede_id,),
        ).fetchone()
        if row is None:
            return {
                "institucion": "", "municipio": "",
                "orientador_nombre": "", "orientador_email": "",
                "orientador_telefono": "", "rector_email": "",
            }
        return dict(row)


# ── API pública: emails de notificación ───────────────────────────────────────

def get_orientador_email(sede_id: int) -> Optional[str]:
    if _use_supabase():
        r = (
            _get_supabase()
            .table("sedes")
            .select("instituciones(orientador_email)")
            .eq("id", sede_id)
            .limit(1)
            .execute()
        )
        if r.data:
            return (r.data[0].get("instituciones") or {}).get("orientador_email")
        return None

    _ensure_sqlite()
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT i.orientador_email
            FROM   sedes s JOIN instituciones i ON s.institucion_id = i.id
            WHERE  s.id = ?
            """,
            (sede_id,),
        ).fetchone()
        return row[0] if row else None


def get_rector_email(sede_id: int) -> Optional[str]:
    if _use_supabase():
        r = (
            _get_supabase()
            .table("sedes")
            .select("instituciones(rector_email)")
            .eq("id", sede_id)
            .limit(1)
            .execute()
        )
        if r.data:
            return (r.data[0].get("instituciones") or {}).get("rector_email")
        return None

    _ensure_sqlite()
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT i.rector_email
            FROM   sedes s JOIN instituciones i ON s.institucion_id = i.id
            WHERE  s.id = ?
            """,
            (sede_id,),
        ).fetchone()
        return row[0] if row else None


def update_notificaciones_alerta(
    alerta_id: str,
    orientador: bool,
    rector: bool,
    peas: bool,
) -> None:
    now = datetime.now(timezone.utc).isoformat()

    if _use_supabase():
        _get_supabase().table("alertas").update({
            "notificado_orientador":  orientador,
            "notificado_rector":      rector,
            "notificado_peas":        peas,
            "timestamp_notificacion": now,
        }).eq("id", alerta_id).execute()
        return

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE alertas
            SET    notificado_orientador  = ?,
                   notificado_rector      = ?,
                   notificado_peas        = ?,
                   timestamp_notificacion = ?
            WHERE  id = ?
            """,
            (int(orientador), int(rector), int(peas), now, alerta_id),
        )


# ── API pública: administradores ──────────────────────────────────────────────

def crear_administrador(
    nombre: str,
    email: str,
    rol: str,
    institucion_id: Optional[int] = None,
    municipio_id: Optional[int] = None,
    password: Optional[str] = None,
) -> str:
    """
    Crea un administrador y retorna su UUID. Si está en Supabase y se envía password, lo crea en Auth.
    rol: 'fcc' | 'orientador' | 'secretaria' | 'rector'
    institucion_id: requerido para 'orientador' y 'rector'.
    municipio_id: opcional para 'secretaria'.
    """
    admin_id = str(uuid.uuid4())
    email = email.lower().strip()

    if _use_supabase():
        client = _get_supabase()
        if password:
            try:
                res = client.auth.admin.create_user({
                    "email": email,
                    "password": password,
                    "email_confirm": True
                })
                admin_id = res.user.id
            except Exception as e:
                raise ValueError(f"Error creando usuario en Supabase Auth: {str(e)}")

        client.table("administradores").insert({
            "id":             admin_id,
            "nombre":         nombre,
            "email":          email,
            "rol":            rol,
            "institucion_id": institucion_id,
            "municipio_id":   municipio_id,
        }).execute()
        return admin_id

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO administradores (id, nombre, email, rol, institucion_id, municipio_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (admin_id, nombre, email, rol, institucion_id, municipio_id),
        )
    return admin_id

def login_admin_supabase(email: str, password: str) -> Optional[dict]:
    email = email.lower().strip()
    if _use_supabase():
        from supabase import create_client
        login_client = create_client(
            st.secrets["SUPABASE_URL"],
            st.secrets["SUPABASE_SERVICE_KEY"],
        )
        try:
            res = login_client.auth.sign_in_with_password({"email": email, "password": password})
            return get_administrador_por_id(res.user.id)
        except Exception:
            return None
    return None

def update_admin_password(admin_id: str, new_password: str) -> bool:
    """Actualiza la contraseña de un administrador en Supabase Auth usando la llave de servicio"""
    if _use_supabase():
        client = _get_supabase()
        try:
            client.auth.admin.update_user_by_id(admin_id, {"password": new_password})
            return True
        except Exception as e:
            raise ValueError(f"Error actualizando contraseña: {str(e)}")
    return False

def get_administrador_por_id(admin_id: str) -> Optional[dict]:
    if _use_supabase():
        r = (
            _get_supabase().table("administradores")
            .select("*")
            .eq("id", admin_id)
            .eq("activo", True)
            .limit(1)
            .execute()
        )
        return r.data[0] if r.data else None

    _ensure_sqlite()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM administradores WHERE id = ? AND activo = 1",
            (admin_id,),
        ).fetchone()
        return dict(row) if row else None


def get_administrador_por_email(email: str) -> Optional[dict]:
    email = email.lower().strip()

    if _use_supabase():
        r = (
            _get_supabase().table("administradores")
            .select("*")
            .eq("email", email)
            .eq("activo", True)
            .limit(1)
            .execute()
        )
        return r.data[0] if r.data else None

    _ensure_sqlite()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM administradores WHERE email = ? AND activo = 1",
            (email,),
        ).fetchone()
        return dict(row) if row else None


def get_todos_administradores() -> list[dict]:
    """Retorna todos los administradores registrados para la gestión del FCC"""
    if _use_supabase():
        r = (
            _get_supabase().table("administradores")
            .select("id, nombre, email, rol, activo, instituciones(nombre), municipios(nombre)")
            .order("nombre")
            .execute()
        )
        result = []
        for row in r.data:
            inst = row.get("instituciones") or {}
            mun = row.get("municipios") or {}
            result.append({
                "id": row["id"],
                "nombre": row["nombre"],
                "email": row["email"],
                "rol": row["rol"],
                "activo": bool(row["activo"]),
                "institucion": inst.get("nombre", ""),
                "municipio": mun.get("nombre", "")
            })
        return result

    _ensure_sqlite()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.nombre, a.email, a.rol, a.activo,
                   i.nombre AS institucion, m.nombre AS municipio
            FROM administradores a
            LEFT JOIN instituciones i ON a.institucion_id = i.id
            LEFT JOIN municipios m ON a.municipio_id = m.id
            ORDER BY a.nombre
            """
        ).fetchall()
        return [dict(r) for r in rows]

def toggle_estado_administrador(admin_id: str, activo: bool) -> bool:
    """Activa o desactiva (borrado lógico) a un administrador"""
    if _use_supabase():
        client = _get_supabase()
        try:
            client.table("administradores").update({"activo": activo}).eq("id", admin_id).execute()
            return True
        except Exception:
            return False

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute("UPDATE administradores SET activo = ? WHERE id = ?", (int(activo), admin_id))
        return True

def eliminar_administrador_permanente(admin_id: str) -> bool:
    """Borra físicamente a un administrador de la base de datos y de Supabase Auth."""
    if _use_supabase():
        client = _get_supabase()
        try:
            # Primero borrar el registro público
            client.table("administradores").delete().eq("id", admin_id).execute()
            # Luego borrar de Supabase Auth usando la llave maestra de admin
            client.auth.admin.delete_user(admin_id)
            return True
        except Exception as e:
            raise ValueError(f"Error borrando administrador: {e}")

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute("DELETE FROM administradores WHERE id = ?", (admin_id,))
        return True

# ── API pública: registro por administrador ───────────────────────────────────

def crear_estudiante_admin(
    nombre: str,
    apellido: str,
    grado: int,
    sede_id: int,
    admin_uuid: str,
    email: Optional[str] = None,
    celular_hash: Optional[str] = None,
    celular: Optional[str] = None,
) -> str:
    """
    Crea un estudiante desde el dashboard del administrador.
    Requiere al menos email o celular_hash.
    celular: número en texto plano para WhatsApp (Opción B piloto — ver DEUDA TÉCNICA 1).
    consentimiento_acudiente_verificado se marca por separado con
    set_consentimiento_acudiente() una vez confirmada la firma física.
    Retorna el estudiante_id generado (ej. 'ALC-9-2026-0042').
    """
    if not email and not celular_hash:
        raise ValueError(
            "Ingresá al menos un medio de contacto: email o número de celular."
        )

    email_norm = email.lower().strip() if email else None
    celular_encriptado = encriptar_celular(celular) if celular else None
    new_uuid = str(uuid.uuid4())
    from datetime import date as _date
    retencion = _date(datetime.now().year + 1, 12, 31).isoformat()

    if _use_supabase():
        est_id = _get_supabase().rpc(
            "generar_estudiante_id", {"p_sede_id": sede_id, "p_grado": grado}
        ).execute().data
        _get_supabase().table("estudiantes").insert({
            "id":                        new_uuid,
            "estudiante_id":             est_id,
            "nombre":                    nombre,
            "apellido":                  apellido,
            "grado":                     grado,
            "email":                     email_norm,
            "celular_hash":              celular_hash,
            "celular":                   celular,
            "sede_id":                   sede_id,
            "administrador_registro_id": admin_uuid,
            "fecha_retencion_hasta":     retencion,
        }).execute()
        return est_id

    _ensure_sqlite()
    with _conn() as conn:
        est_id = _generar_id_sqlite(conn, sede_id, grado)
        conn.execute(
            """
            INSERT INTO estudiantes
                (id, estudiante_id, nombre, apellido, grado,
                 email, celular_hash, celular, sede_id, administrador_registro_id,
                 fecha_retencion_hasta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (new_uuid, est_id, nombre, apellido, grado,
             email_norm, celular_hash, celular, sede_id, admin_uuid, retencion),
        )
    return est_id


def set_consentimiento_acudiente(estudiante_uuid: str) -> None:
    """
    Marca que la autorización del acudiente fue verificada físicamente.
    Solo el administrador puede llamar este método desde el dashboard.
    Sin esta marca, el estudiante no puede acceder al chat (auth.py lo bloquea).
    """
    now = datetime.now(timezone.utc).isoformat()

    if _use_supabase():
        _get_supabase().table("estudiantes").update({
            "consentimiento_acudiente_verificado": True,
            "fecha_verificacion_acudiente":        now,
        }).eq("id", estudiante_uuid).execute()
        return

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE estudiantes
            SET    consentimiento_acudiente_verificado = 1,
                   fecha_verificacion_acudiente        = ?
            WHERE  id = ?
            """,
            (now, estudiante_uuid),
        )


# ── API pública: dashboard de administrador ───────────────────────────────────

def update_consentimiento_url(estudiante_uuid: str, url: str) -> None:
    if _use_supabase():
        _get_supabase().table("estudiantes").update(
            {"consentimiento_archivo_url": url}
        ).eq("id", estudiante_uuid).execute()
        return
    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            "UPDATE estudiantes SET consentimiento_archivo_url = ? WHERE id = ?",
            (url, estudiante_uuid),
        )


def guardar_archivo_consentimiento(
    estudiante_id: str,
    sede_id: int,
    extension: str,
    file_bytes: bytes,
) -> str:
    """
    Persiste el archivo del consentimiento del acudiente.
    Supabase: sube al bucket 'consentimientos', retorna la URL pública.
    SQLite:   guarda en carpeta local 'consentimientos/', retorna la ruta relativa.
    Nota: el bucket 'consentimientos' debe crearse manualmente en Supabase antes
    de usar esta función en producción.
    """
    import os
    extension = extension.lower().lstrip(".")
    ruta = f"{sede_id}/{estudiante_id}.{extension}"

    if _use_supabase():
        _get_supabase().storage.from_("consentimientos").upload(
            ruta,
            file_bytes,
            {"content-type": f"image/{extension}" if extension != "pdf" else "application/pdf"},
        )
        return ruta

    os.makedirs("consentimientos", exist_ok=True)
    local_path = f"consentimientos/{estudiante_id}.{extension}"
    with open(local_path, "wb") as fh:
        fh.write(file_bytes)
    return local_path

def get_consentimiento_signed_url(ruta_o_url: str) -> str:
    """
    Retorna una URL firmada (válida por 60s) para ver el PDF/imagen de forma segura.
    Si recibe una URL pública vieja, intenta extraer la ruta.
    Si es un entorno local, retorna la ruta local tal cual.
    """
    if not ruta_o_url:
        return ""
    if not _use_supabase():
        return ruta_o_url
        
    ruta = ruta_o_url
    if ruta.startswith("http"):
        if "/storage/v1/object/public/consentimientos/" in ruta:
            ruta = ruta.split("/storage/v1/object/public/consentimientos/")[-1]
        else:
            return ruta_o_url # URL de otro origen desconocido
            
    try:
        # Generar signed url por 60 segundos
        res = _get_supabase().storage.from_("consentimientos").create_signed_url(ruta, 60)
        return res["signedURL"]
    except Exception:
        return ruta_o_url


def get_sedes_disponibles(admin_uuid: str, rol: str) -> list:
    """
    Retorna las sedes visibles para el admin según su rol.
    Cada dict: {id, nombre, municipio, institucion}
    fcc / secretaria: todas las sedes.
    orientador:       solo sedes de su institución.
    """
    _SQL_ALL = """
        SELECT s.id, s.nombre, m.nombre AS municipio, i.nombre AS institucion
        FROM   sedes s
        JOIN   instituciones i ON s.institucion_id = i.id
        JOIN   municipios    m ON i.municipio_id   = m.id
        ORDER  BY m.nombre, i.nombre, s.nombre
    """
    _SQL_ORIENTADOR = """
        SELECT s.id, s.nombre, m.nombre AS municipio, i.nombre AS institucion
        FROM   sedes s
        JOIN   instituciones i ON s.institucion_id = i.id
        JOIN   municipios    m ON i.municipio_id   = m.id
        WHERE  i.id = (SELECT institucion_id FROM administradores WHERE id = ?)
        ORDER  BY s.nombre
    """

    if _use_supabase():
        sb = _get_supabase()
        if rol == "orientador":
            adm = sb.table("administradores").select("institucion_id").eq("id", admin_uuid).limit(1).execute()
            inst_id = adm.data[0]["institucion_id"] if adm.data else None
            if not inst_id:
                return []
            r = sb.table("sedes").select(
                "id, nombre, instituciones(nombre, municipios(nombre))"
            ).eq("institucion_id", inst_id).order("nombre").execute()
        else:
            r = sb.table("sedes").select(
                "id, nombre, instituciones(nombre, municipios(nombre))"
            ).execute()
        result = []
        for row in r.data:
            inst = row.get("instituciones") or {}
            mun  = inst.get("municipios") or {}
            result.append({
                "id":         row["id"],
                "nombre":     row["nombre"],
                "municipio":  mun.get("nombre", ""),
                "institucion": inst.get("nombre", ""),
            })
        result.sort(key=lambda x: (x["municipio"], x["institucion"], x["nombre"]))
        return result

    _ensure_sqlite()
    with _conn() as conn:
        if rol == "orientador":
            rows = conn.execute(_SQL_ORIENTADOR, (admin_uuid,)).fetchall()
        else:
            rows = conn.execute(_SQL_ALL).fetchall()
        return [dict(r) for r in rows]


def get_estudiantes_por_admin(admin_uuid: str, rol: str) -> list:
    """
    Retorna estudiantes dentro del scope del admin.
    Cada dict: estudiante_id, nombre, apellido, grado, sede_nombre, institucion,
    municipio, sesion_actual, perfil_riesgo,
    consentimiento_acudiente_verificado, tiene_archivo_consentimiento.
    """
    _BASE = """
        SELECT e.id,
               e.estudiante_id,
               e.nombre,
               e.apellido,
               e.grado,
               e.sede_id,
               e.mentoria_completada,
               s.nombre  AS sede_nombre,
               i.nombre  AS institucion,
               m.nombre  AS municipio,
               e.sesion_actual,
               e.momento_actual,
               e.perfil_riesgo,
               e.consentimiento_acudiente_verificado,
               CASE WHEN e.consentimiento_archivo_url IS NOT NULL THEN 1 ELSE 0 END
                   AS tiene_archivo_consentimiento
        FROM   estudiantes   e
        JOIN   sedes         s ON e.sede_id          = s.id
        JOIN   instituciones i ON s.institucion_id   = i.id
        JOIN   municipios    m ON i.municipio_id     = m.id
        {where}
        ORDER  BY m.nombre, i.nombre, e.apellido, e.nombre
    """

    if _use_supabase():
        sb = _get_supabase()
        sel = (
            "id, estudiante_id, nombre, apellido, grado, sede_id, mentoria_completada, "
            "sesion_actual, momento_actual, perfil_riesgo, consentimiento_acudiente_verificado, "
            "consentimiento_archivo_url, "
            "sedes(nombre, instituciones(nombre, municipios(nombre)))"
        )
        q = sb.table("estudiantes").select(sel)
        if rol == "orientador" or rol == "rector":
            adm = sb.table("administradores").select("institucion_id").eq("id", admin_uuid).limit(1).execute()
            inst_id = adm.data[0]["institucion_id"] if adm.data else None
            if inst_id:
                # PostgREST inner join syntax on foreign tables is limited, 
                # but doing eq on referenced tables effectively filters.
                # Actually, filtering a child table by parent table id in supabase-py:
                # We can just fetch all and filter in memory, or use `not.is.null`
                pass # Wait, eq("sedes.institucion_id") filters the nested select, but does NOT filter the root rows!
        
        rows_raw = q.order("apellido").execute().data
        
        # Necesitamos inst_id y mun_id del admin
        adm = sb.table("administradores").select("institucion_id, municipio_id").eq("id", admin_uuid).limit(1).execute()
        inst_id_admin = adm.data[0]["institucion_id"] if adm.data else None
        mun_id_admin = adm.data[0]["municipio_id"] if adm.data else None

        result = []
        for row in rows_raw:
            sede_info = row.get("sedes") or {}
            inst_info = sede_info.get("instituciones") or {}
            mun_info  = inst_info.get("municipios") or {}
            
            # Filtro en memoria por si acaso (porque el inner join de supabase a veces no oculta la fila root)
            if (rol == "orientador" or rol == "rector") and inst_info.get("id") != inst_id_admin:
                continue
            if rol == "secretaria" and mun_id_admin is not None and mun_info.get("id") != mun_id_admin:
                continue
                
            result.append({
                "id":                               row["id"],
                "estudiante_id":                    row["estudiante_id"],
                "nombre":                           row["nombre"],
                "apellido":                         row["apellido"],
                "grado":                            row["grado"],
                "sede_id":                          row["sede_id"],
                "mentoria_completada":              bool(row.get("mentoria_completada")),
                "sede_nombre":                      sede_info.get("nombre", ""),
                "institucion":                      inst_info.get("nombre", ""),
                "municipio":                        mun_info.get("nombre", ""),
                "sesion_actual":                    row["sesion_actual"],
                "momento_actual":                   row["momento_actual"],
                "perfil_riesgo":                    row["perfil_riesgo"],
                "consentimiento_acudiente_verificado": row["consentimiento_acudiente_verificado"],
                "tiene_archivo_consentimiento":     bool(row.get("consentimiento_archivo_url")),
                "consentimiento_archivo_url":       row.get("consentimiento_archivo_url"),
            })
        return result

    _ensure_sqlite()
    with _conn() as conn:
        _BASE_SQLITE = _BASE.replace(
            "CASE WHEN e.consentimiento_archivo_url IS NOT NULL THEN 1 ELSE 0 END",
            "e.consentimiento_archivo_url, CASE WHEN e.consentimiento_archivo_url IS NOT NULL THEN 1 ELSE 0 END"
        )
        if rol in ["orientador", "rector"]:
            sql = _BASE_SQLITE.format(
                where="WHERE i.id = (SELECT institucion_id FROM administradores WHERE id = ?)"
            )
            rows = conn.execute(sql, (admin_uuid,)).fetchall()
        elif rol == "secretaria":
            sql = _BASE_SQLITE.format(
                where="WHERE (SELECT municipio_id FROM administradores WHERE id = ?) IS NULL OR m.id = (SELECT municipio_id FROM administradores WHERE id = ?)"
            )
            rows = conn.execute(sql, (admin_uuid, admin_uuid)).fetchall()
        else:
            rows = conn.execute(_BASE_SQLITE.format(where="")).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["tiene_archivo_consentimiento"] = bool(d["tiene_archivo_consentimiento"])
            d["consentimiento_acudiente_verificado"] = bool(d["consentimiento_acudiente_verificado"])
            d["mentoria_completada"] = bool(d.get("mentoria_completada", 0))
            result.append(d)
        return result


def get_alertas_pendientes(admin_uuid: str, rol: str) -> list:
    """
    Retorna alertas con estado='pendiente' dentro del scope del admin.
    Cada dict: id, estudiante_codigo, nombre_estudiante, tipo, timestamp.
    """
    _BASE = """
        SELECT a.id,
               e.estudiante_id AS estudiante_codigo,
               e.nombre || ' ' || e.apellido AS nombre_estudiante,
               a.tipo,
               a.timestamp
        FROM   alertas        a
        JOIN   estudiantes    e ON a.estudiante_id = e.id
        JOIN   sedes          s ON a.sede_id        = s.id
        JOIN   instituciones  i ON s.institucion_id = i.id
        WHERE  a.estado = 'pendiente'
        {scope}
        ORDER  BY a.timestamp DESC
    """

    if _use_supabase():
        sb = _get_supabase()
        sel = (
            "id, tipo, timestamp, estado, "
            "estudiantes(estudiante_id, nombre, apellido), "
            "sedes(institucion_id, instituciones(municipio_id))"
        )
        q = sb.table("alertas").select(sel).eq("estado", "pendiente").order("timestamp", desc=True)
        rows_raw = q.execute().data

        # Obtener ids del admin actual
        adm = sb.table("administradores").select("institucion_id, municipio_id").eq("id", admin_uuid).limit(1).execute()
        inst_id_admin = adm.data[0]["institucion_id"] if adm.data else None
        mun_id_admin = adm.data[0]["municipio_id"] if adm.data else None

        result = []
        for row in rows_raw:
            sede_info = row.get("sedes") or {}
            inst_id_alerta = sede_info.get("institucion_id")
            inst_info = sede_info.get("instituciones") or {}
            mun_id_alerta = inst_info.get("municipio_id")

            # Filtros en memoria
            if (rol == "orientador" or rol == "rector") and inst_id_alerta != inst_id_admin:
                continue
            if rol == "secretaria" and mun_id_admin is not None and mun_id_alerta != mun_id_admin:
                continue

            est = row.get("estudiantes") or {}
            result.append({
                "id":               row["id"],
                "estudiante_codigo": est.get("estudiante_id", ""),
                "nombre_estudiante": f"{est.get('nombre','')} {est.get('apellido','')}".strip(),
                "tipo":             row["tipo"],
                "timestamp":        row["timestamp"],
            })
        return result

    _ensure_sqlite()
    with _conn() as conn:
        if rol in ["orientador", "rector"]:
            sql = _BASE.format(
                scope="AND i.id = (SELECT institucion_id FROM administradores WHERE id = ?)"
            )
            rows = conn.execute(sql, (admin_uuid,)).fetchall()
        elif rol == "secretaria":
            sql = _BASE.format(
                scope="AND ((SELECT municipio_id FROM administradores WHERE id = ?) IS NULL OR i.municipio_id = (SELECT municipio_id FROM administradores WHERE id = ?))"
            )
            rows = conn.execute(sql, (admin_uuid, admin_uuid)).fetchall()
        else:
            rows = conn.execute(_BASE.format(scope="")).fetchall()
        return [dict(r) for r in rows]


def marcar_alerta_vista(alerta_id: str) -> None:
    if _use_supabase():
        _get_supabase().table("alertas").update(
            {"estado": "vista"}
        ).eq("id", alerta_id).execute()
        return
    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            "UPDATE alertas SET estado = 'vista' WHERE id = ?",
            (alerta_id,),
        )


# ── API pública: gestión de instituciones (rol fcc) ───────────────────────────

def get_todas_instituciones() -> list[dict]:
    """
    Retorna todas las instituciones con datos de contacto y sedes.
    Uso exclusivo del rol fcc en el dashboard de administración.
    Cada dict: id, nombre, municipio_nombre, orientador_nombre, orientador_email,
    orientador_telefono, rector_nombre, rector_email, sedes (lista de nombres).
    """
    if _use_supabase():
        r = (
            _get_supabase()
            .table("instituciones")
            .select(
                "id, nombre, orientador_nombre, orientador_email, orientador_telefono, "
                "rector_nombre, rector_email, "
                "municipios(nombre), "
                "sedes(nombre)"
            )
            .order("nombre")
            .execute()
        )
        result = []
        for row in r.data:
            mun   = row.get("municipios") or {}
            sedes = sorted(s["nombre"] for s in (row.get("sedes") or []))
            result.append({
                "id":                  row["id"],
                "nombre":              row["nombre"],
                "municipio_nombre":    mun.get("nombre", ""),
                "orientador_nombre":   row.get("orientador_nombre") or "",
                "orientador_email":    row.get("orientador_email") or "",
                "orientador_telefono": row.get("orientador_telefono") or "",
                "rector_nombre":       row.get("rector_nombre") or "",
                "rector_email":        row.get("rector_email") or "",
                "sedes":               sedes,
            })
        result.sort(key=lambda x: (x["municipio_nombre"], x["nombre"]))
        return result

    _ensure_sqlite()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT i.id                  AS inst_id,
                   i.nombre              AS inst_nombre,
                   i.orientador_nombre,
                   i.orientador_email,
                   i.orientador_telefono,
                   i.rector_nombre,
                   i.rector_email,
                   m.nombre              AS municipio_nombre,
                   s.nombre              AS sede_nombre
            FROM   instituciones  i
            JOIN   municipios     m ON i.municipio_id   = m.id
            LEFT JOIN sedes       s ON s.institucion_id = i.id
            ORDER  BY m.nombre, i.nombre, s.nombre
            """
        ).fetchall()

    from collections import OrderedDict
    instituciones: "OrderedDict[int, dict]" = OrderedDict()
    for row in rows:
        iid = row["inst_id"]
        if iid not in instituciones:
            instituciones[iid] = {
                "id":                  iid,
                "nombre":              row["inst_nombre"],
                "municipio_nombre":    row["municipio_nombre"] or "",
                "orientador_nombre":   row["orientador_nombre"] or "",
                "orientador_email":    row["orientador_email"] or "",
                "orientador_telefono": row["orientador_telefono"] or "",
                "rector_nombre":       row["rector_nombre"] or "",
                "rector_email":        row["rector_email"] or "",
                "sedes":               [],
            }
        if row["sede_nombre"]:
            instituciones[iid]["sedes"].append(row["sede_nombre"])
    return list(instituciones.values())


def update_institucion(inst_id: int, datos: dict) -> None:
    """
    Actualiza datos de contacto de una institución.
    datos: dict con claves orientador_nombre, orientador_email, orientador_telefono,
    rector_nombre, rector_email (todos opcionales; None para limpiar el campo).
    """
    if _use_supabase():
        _get_supabase().table("instituciones").update(datos).eq("id", inst_id).execute()
        return

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE instituciones
            SET    orientador_nombre    = ?,
                   orientador_email     = ?,
                   orientador_telefono  = ?,
                   rector_nombre        = ?,
                   rector_email         = ?
            WHERE  id = ?
            """,
            (
                datos.get("orientador_nombre"),
                datos.get("orientador_email"),
                datos.get("orientador_telefono"),
                datos.get("rector_nombre"),
                datos.get("rector_email"),
                inst_id,
            ),
        )


# ── API pública: supresión y retención (P3 + P4, Ley 1581/2012) ──────────────

def suprimir_estudiante(estudiante_uuid: str, motivo: str) -> None:
    """
    Ejecuta el derecho de supresión (Ley 1581/2012).
    1. Elimina todos los mensajes del estudiante.
    2. Elimina todas las alertas del estudiante.
    3. Anonimiza el registro: nombre/apellido → 'SUPRIMIDO', email/celular_hash → NULL.
    Conserva municipio, grado, sesion_actual y perfil_riesgo para estadísticas agregadas.
    Solo el rol fcc puede llamar este método desde el dashboard.
    """
    now = datetime.now(timezone.utc).isoformat()

    if _use_supabase():
        sb = _get_supabase()
        sb.table("envios_ficha").delete().eq("estudiante_id", estudiante_uuid).execute()
        sb.table("whatsapp_mensajes").delete().eq("estudiante_id", estudiante_uuid).execute()
        sb.table("mensajes").delete().eq("estudiante_id", estudiante_uuid).execute()
        sb.table("alertas").delete().eq("estudiante_id", estudiante_uuid).execute()
        sb.table("estudiantes").update({
            "suprimido":        True,
            "nombre":           "SUPRIMIDO",
            "apellido":         "SUPRIMIDO",
            "email":            None,
            "celular_hash":     None,
            "celular":          None,
            "fecha_supresion":  now,
            "motivo_supresion": motivo,
        }).eq("id", estudiante_uuid).execute()
        return

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute("DELETE FROM envios_ficha       WHERE estudiante_id = ?", (estudiante_uuid,))
        conn.execute("DELETE FROM whatsapp_mensajes  WHERE estudiante_id = ?", (estudiante_uuid,))
        conn.execute("DELETE FROM mensajes           WHERE estudiante_id = ?", (estudiante_uuid,))
        conn.execute("DELETE FROM alertas            WHERE estudiante_id = ?", (estudiante_uuid,))
        conn.execute(
            """
            UPDATE estudiantes
            SET    suprimido        = 1,
                   nombre           = 'SUPRIMIDO',
                   apellido         = 'SUPRIMIDO',
                   email            = NULL,
                   celular_hash     = NULL,
                   celular          = NULL,
                   fecha_supresion  = ?,
                   motivo_supresion = ?
            WHERE  id = ?
            """,
            (now, motivo, estudiante_uuid),
        )


def get_estudiantes_vencidos() -> list[dict]:
    """
    Retorna estudiantes cuyo período de retención expiró (fecha_retencion_hasta < hoy)
    y que aún no han sido suprimidos.
    Cada dict: estudiante_id, nombre, apellido, grado, fecha_retencion_hasta, municipio.
    Uso exclusivo del rol fcc para mostrar el banner de alerta en el dashboard.
    """
    from datetime import date as _date
    today = _date.today().isoformat()

    if _use_supabase():
        r = (
            _get_supabase()
            .table("estudiantes")
            .select(
                "estudiante_id, nombre, apellido, grado, fecha_retencion_hasta, "
                "sedes(instituciones(municipios(nombre)))"
            )
            .lt("fecha_retencion_hasta", today)
            .eq("suprimido", False)
            .order("fecha_retencion_hasta")
            .execute()
        )
        result = []
        for row in r.data:
            sede_info = row.get("sedes") or {}
            inst_info = sede_info.get("instituciones") or {}
            mun_info  = inst_info.get("municipios") or {}
            result.append({
                "estudiante_id":          row["estudiante_id"],
                "nombre":                 row["nombre"],
                "apellido":               row["apellido"],
                "grado":                  row["grado"],
                "fecha_retencion_hasta":  row["fecha_retencion_hasta"],
                "municipio":              mun_info.get("nombre", ""),
            })
        return result

    _ensure_sqlite()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT e.estudiante_id,
                   e.nombre,
                   e.apellido,
                   e.grado,
                   e.fecha_retencion_hasta,
                   m.nombre AS municipio
            FROM   estudiantes   e
            JOIN   sedes         s ON e.sede_id         = s.id
            JOIN   instituciones i ON s.institucion_id  = i.id
            JOIN   municipios    m ON i.municipio_id    = m.id
            WHERE  e.fecha_retencion_hasta < ?
              AND  e.suprimido = 0
            ORDER  BY e.fecha_retencion_hasta
            """,
            (today,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── API pública: WhatsApp re-engagement (P14) ─────────────────────────────────

def registrar_whatsapp_mensaje(
    estudiante_uuid: str,
    mensaje_numero: int,
    estado: str,
) -> None:
    """Registra un mensaje WhatsApp enviado (o fallido) en la tabla whatsapp_mensajes."""
    msg_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    if _use_supabase():
        _get_supabase().table("whatsapp_mensajes").insert({
            "id":             msg_id,
            "estudiante_id":  estudiante_uuid,
            "mensaje_numero": mensaje_numero,
            "estado":         estado,
            "enviado_at":     now,
        }).execute()
        return

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_mensajes (id, estudiante_id, mensaje_numero, estado, enviado_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (msg_id, estudiante_uuid, mensaje_numero, estado, now),
        )


def _aplicar_reglas_reengagement(
    estudiantes: list[dict],
    wa_enviados: dict,
    ultimo_chat: dict,
    umbral_1d: str,
    umbral_2d: str,
    umbral_5d: str,
) -> list[dict]:
    """
    Aplica las 5 reglas de re-engagement (backlog PENDIENTE 14) y retorna
    los candidatos con su mensaje asignado.
    MSG5 solo se asigna si no aplica ningún MSG1-4 en esta pasada.
    """
    resultado = []
    for e in estudiantes:
        uid = e["id"]
        ya_enviados = wa_enviados.get(uid, set())
        ultimo = ultimo_chat.get(uid)
        sesion = e["sesion_actual"]
        momento = e["momento_actual"]
        registrado = e.get("fecha_registro") or ""

        asignado = None

        if sesion == 1 and momento == 1 and 1 not in ya_enviados:
            if registrado and registrado < umbral_1d and ultimo is None:
                asignado = 1
        elif sesion == 2 and momento == 1 and 2 not in ya_enviados:
            if ultimo and ultimo < umbral_2d:
                asignado = 2
        elif sesion == 3 and momento == 1 and 3 not in ya_enviados:
            if ultimo and ultimo < umbral_2d:
                asignado = 3
        elif sesion == 4 and momento == 1 and 4 not in ya_enviados:
            if ultimo and ultimo < umbral_2d:
                asignado = 4

        if asignado is None and 5 not in ya_enviados:
            ref = ultimo or registrado
            if ref and ref < umbral_5d:
                asignado = 5

        if asignado is not None:
            resultado.append({
                "id":             uid,
                "nombre":         e["nombre"],
                "estudiante_id":  e["estudiante_id"],
                "celular":        e["celular"],
                "mensaje_numero": asignado,
            })

    return resultado


def get_estudiantes_para_reengagement() -> list[dict]:
    """
    Retorna candidatos elegibles para re-engagement por WhatsApp.
    Solo estudiantes con celular IS NOT NULL, mentoria_completada=FALSE, suprimido=FALSE.
    Aplica las 5 reglas del backlog PENDIENTE 14.
    Cada dict: id, nombre, estudiante_id, celular, mensaje_numero
    """
    from datetime import datetime as _dt, timedelta
    now = _dt.now(timezone.utc)
    umbral_1d = (now - timedelta(days=1)).isoformat()
    umbral_2d = (now - timedelta(days=1)).isoformat()
    umbral_5d = (now - timedelta(days=3)).isoformat()

    if _use_supabase():
        sb = _get_supabase()
        r_est = (
            sb.table("estudiantes")
            .select("id, nombre, estudiante_id, celular, sesion_actual, momento_actual, fecha_registro")
            .eq("mentoria_completada", False)
            .eq("suprimido", False)
            .not_.is_("celular", "null")
            .execute()
        )
        if not r_est.data:
            return []
            
        est_data = r_est.data
        for e in est_data:
            e["celular"] = desencriptar_celular(e["celular"])
            
        ids = [e["id"] for e in est_data]

        r_wa = sb.table("whatsapp_mensajes").select("estudiante_id, mensaje_numero").in_("estudiante_id", ids).execute()
        wa_enviados: dict = {}
        for row in r_wa.data:
            wa_enviados.setdefault(row["estudiante_id"], set()).add(row["mensaje_numero"])

        r_msg = sb.table("mensajes").select("estudiante_id, timestamp").in_("estudiante_id", ids).execute()
        ultimo_chat: dict = {}
        for row in r_msg.data:
            uid = row["estudiante_id"]
            ts = row["timestamp"]
            if uid not in ultimo_chat or ts > ultimo_chat[uid]:
                ultimo_chat[uid] = ts

        return _aplicar_reglas_reengagement(est_data, wa_enviados, ultimo_chat, umbral_1d, umbral_2d, umbral_5d)

    _ensure_sqlite()
    with _conn() as conn:
        rows_est_raw = conn.execute(
            """
            SELECT id, nombre, estudiante_id, celular, sesion_actual, momento_actual, fecha_registro
            FROM   estudiantes
            WHERE  mentoria_completada = 0
              AND  suprimido = 0
              AND  celular IS NOT NULL
            """
        ).fetchall()
        if not rows_est_raw:
            return []

        rows_est = []
        for r in rows_est_raw:
            d = dict(r)
            d["celular"] = desencriptar_celular(d["celular"])
            rows_est.append(d)

        ids = [r["id"] for r in rows_est]
        placeholders = ",".join("?" * len(ids))

        rows_wa = conn.execute(
            f"SELECT estudiante_id, mensaje_numero FROM whatsapp_mensajes WHERE estudiante_id IN ({placeholders})",
            ids,
        ).fetchall()
        wa_enviados = {}
        for row in rows_wa:
            wa_enviados.setdefault(row["estudiante_id"], set()).add(row["mensaje_numero"])

        rows_msg = conn.execute(
            f"""
            SELECT estudiante_id, MAX(timestamp) AS ultimo
            FROM   mensajes
            WHERE  estudiante_id IN ({placeholders})
            GROUP  BY estudiante_id
            """,
            ids,
        ).fetchall()
        ultimo_chat = {r["estudiante_id"]: r["ultimo"] for r in rows_msg}

    return _aplicar_reglas_reengagement(
        [dict(r) for r in rows_est], wa_enviados, ultimo_chat, umbral_1d, umbral_2d, umbral_5d
    )


# ── API pública: envíos de ficha al orientador ────────────────────────────────

def registrar_envio_ficha(estudiante_id: str, orientador_email: str, exito: bool) -> None:
    """Registra el intento de envío de la ficha de acompañamiento por email."""
    if _use_supabase():
        _get_supabase().table("envios_ficha").insert({
            "estudiante_id":    estudiante_id,
            "orientador_email": orientador_email,
            "exito":            exito,
        }).execute()
        return

    _ensure_sqlite()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO envios_ficha (estudiante_id, orientador_email, exito)
               VALUES (?, ?, ?)""",
            (estudiante_id, orientador_email, int(exito)),
        )


def get_envio_ficha(estudiante_id: str) -> Optional[dict]:
    """Retorna el último envío de ficha para un estudiante, o None si no hay."""
    if _use_supabase():
        result = (
            _get_supabase().table("envios_ficha")
            .select("*")
            .eq("estudiante_id", estudiante_id)
            .order("timestamp", desc=True)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    _ensure_sqlite()
    with _conn() as conn:
        row = conn.execute(
            """SELECT * FROM envios_ficha
               WHERE estudiante_id = ?
               ORDER BY timestamp DESC LIMIT 1""",
            (estudiante_id,),
        ).fetchone()
        return dict(row) if row else None


def subir_pdf_temporal_whatsapp(pdf_bytes: bytes) -> Optional[str]:
    """
    Sube el PDF temporalmente al bucket público (consentimientos) con nombre ofuscado
    y retorna la URL pública para poder enviarlo vía Twilio WhatsApp.
    """
    import uuid
    if not _use_supabase():
        return None

    nombre_archivo = f"temp_mapa_{uuid.uuid4().hex}.pdf"
    ruta = f"whatsapp/{nombre_archivo}"
    try:
        _get_supabase().storage.from_("consentimientos").upload(
            ruta,
            pdf_bytes,
            {"content-type": "application/pdf"}
        )
        return _get_supabase().storage.from_("consentimientos").get_public_url(ruta)
    except Exception as e:
        import logging
        logging.error("Error subiendo PDF temporal a Supabase: %s", e)
        return None
