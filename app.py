import streamlit as st

# ── Configuración de página ────────────────────────────────────────────────────
st.set_page_config(
    page_title="rAÍz - Guía de Proyecto de Vida",
    page_icon="🌱",
    layout="wide",
)

# ── Admin gate ─────────────────────────────────────────────────────────────────
# Evaluar antes de cualquier import pesado para evitar efectos secundarios
# en el primer render de Streamlit Cloud.
# query_params puede retornar str o list según versión — normalizamos.
_admin_val = st.query_params.get("admin", "")
if isinstance(_admin_val, list):
    _admin_val = _admin_val[0] if _admin_val else ""
if _admin_val == "1":
    from admin_dashboard import mostrar_dashboard_admin
    mostrar_dashboard_admin()
    st.stop()

from google import genai
from google.genai import types

import auth
import database as db
from streamlit_sortables import sort_items

# ── Cliente Gemini ─────────────────────────────────────────────────────────────
try:
    API_KEY = st.secrets["GEMINI_API_KEY"]
except KeyError:
    st.error("Falta configurar GEMINI_API_KEY en .streamlit/secrets.toml")
    st.stop()

MODEL = "gemini-3.1-flash-lite"

@st.cache_resource
def obtener_cliente():
    return genai.Client(api_key=API_KEY)

client = obtener_cliente()

# ── System prompt ──────────────────────────────────────────────────────────────
@st.cache_data
def load_instructions() -> str:
    try:
        with open("instrucciones.txt", "r", encoding="utf-8") as f:
            content = f.read().strip()
            return content if content else "Eres un asistente útil. (Nota: Archivo vacío)."
    except FileNotFoundError:
        return "Eres un asistente útil. (Nota: Archivo no encontrado)."

system_instruction = load_instructions()

# ── Autenticación ──────────────────────────────────────────────────────────────
if not auth.esta_autenticado():
    st.title("🌱 rAÍz")
    st.subheader("Tu Guía de Proyecto de Vida")
    auth.mostrar_pantalla_auth()
    st.stop()

# ── A partir de aquí: estudiante autenticado ──────────────────────────────────
estudiante = st.session_state.estudiante

# Guard de acudiente: auth.py ya bloquea esto, pero lo verificamos aquí también
# por si el registro viene de una sesión anterior al piloto.
if not estudiante.get("consentimiento_acudiente_verificado"):
    st.error(
        "Tu perfil está pendiente de activación por parte de tu institución. "
        "Pedile a tu orientador/a que habilite tu acceso."
    )
    st.stop()

st.markdown(
    "<style>.block-container{max-width:800px !important;padding-left:2rem !important;"
    "padding-right:2rem !important;}</style>",
    unsafe_allow_html=True,
)
st.title("🌱 rAÍz")
st.caption(f"**{estudiante['nombre']}** · ID: `{estudiante['estudiante_id']}`")

# ── Limpiador de etiquetas internas ───────────────────────────────────────────
# Las etiquetas se eliminan SOLO en pantalla.
# En DB se guarda el texto crudo para que Gemini reconstruya coherentemente
# el contexto pedagógico y de riesgo al retomar sesión.
# Se usa regex en lugar de replace() por dos razones:
#   1. Tolera espacios accidentales dentro de los corchetes (ej. "[ FIN_CONSEJERIA ]")
#   2. Cubre cualquier etiqueta futura en mayúsculas sin modificar esta función.
import re as _re

_PATRON_ETIQUETAS = _re.compile(
    r"\[\s*(?:FIN_CONSEJERIA|RIESGO_BAJO|RIESGO_MEDIO|RIESGO_ALTO"
    r"|ALERTA_ORIENTADOR_REQUERIDA|ALERTA_PSICOLOGICA_CRITICA"
    r"|INICIAR_RONDA_RELAMPAGO"
    r"|PROGRESO:S\d+M\d+)\s*\]"
    r"|Nota interna de rAÍz\s*:?[^\n]*"
    r"|Nota interna\s*:[^\n]*"
    r"|\(\)",
    _re.IGNORECASE,
)

def limpiar_etiquetas(texto: str) -> str:
    return _PATRON_ETIQUETAS.sub("", texto).strip()

# ── Helper: procesar etiquetas internas ───────────────────────────────────────
# Definida antes de los bloques de interacción para evitar errores de orden.
def _procesar_etiquetas(raw: str, est: dict, sesion_actual: int) -> bool:
    """
    Procesa todas las etiquetas internas de una respuesta del modelo.
    Actualiza DB según corresponda y retorna True si hay alguna alerta activa.
    """
    tiene_alerta = False

    # ── Alertas ────────────────────────────────────────────────────────────────
    if "[ALERTA_ORIENTADOR_REQUERIDA]" in raw:
        tiene_alerta = True
        db.crear_alerta(
            estudiante_uuid=est["id"],
            sede_id=est["sede_id"],
            tipo="orientador_requerida",
        )

    if "[ALERTA_PSICOLOGICA_CRITICA]" in raw:
        tiene_alerta = True
        alerta_id = db.crear_alerta(
            estudiante_uuid=est["id"],
            sede_id=est["sede_id"],
            tipo="psicologica_critica",
        )
        # Notificación simultánea a tres niveles: orientador, rector, PEAS
        orientador_email = db.get_orientador_email(est["sede_id"])
        rector_email     = db.get_rector_email(est["sede_id"])
        peas_email       = st.secrets.get("PEAS_EMAIL", "")
        try:
            import email_service
            resultados = email_service.enviar_alerta_critica(
                orientador_email=orientador_email,
                rector_email=rector_email,
                peas_email=peas_email,
                nombre_estudiante=f"{est['nombre']} {est['apellido']}",
                estudiante_id=est["estudiante_id"],
            )
            db.update_notificaciones_alerta(alerta_id, **resultados)
        except Exception:
            pass  # La alerta quedó en DB; el dashboard la reintentará

    # ── Perfil de riesgo ───────────────────────────────────────────────────────
    # Evaluado de mayor a menor para capturar siempre el nivel más grave.
    for tag, nivel in [
        ("[RIESGO_ALTO]",  "alto"),
        ("[RIESGO_MEDIO]", "medio"),
        ("[RIESGO_BAJO]",  "bajo"),
    ]:
        if tag in raw:
            db.update_perfil_riesgo(est["id"], nivel)
            st.session_state.estudiante["perfil_riesgo"] = nivel
            break

    # ── Progreso pedagógico ────────────────────────────────────────────────
    progreso_match = _re.search(r"\[PROGRESO:S(\d+)M(\d+)\]", raw)
    if progreso_match:
        nueva_sesion = int(progreso_match.group(1))
        nuevo_momento = int(progreso_match.group(2))
        sesion_prev = est.get("sesion_actual", 1)
        momento_prev = est.get("momento_actual", 1)
        if nueva_sesion != sesion_prev or nuevo_momento != momento_prev:
            db.update_progreso(est["id"], nueva_sesion, nuevo_momento)
            st.session_state.estudiante["sesion_actual"] = nueva_sesion
            st.session_state.estudiante["momento_actual"] = nuevo_momento

    # ── Fin de mentoría ────────────────────────────────────────────────────────
    if "[FIN_CONSEJERIA]" in raw:
        st.session_state.fin_consejeria = True
        db.set_mentoria_completada(est["id"])
        st.session_state.estudiante["mentoria_completada"] = True

    return tiene_alerta

# ── Ronda Relámpago — 7 Escenarios Estándar ──────────────────────────────────
# Test ipsativo: el estudiante arrastra para ordenar 4 opciones por escenario.
# Se activa cuando el modelo emite [INICIAR_RONDA_RELAMPAGO].

SITUACIONES_RONDA = [
    {
        "titulo": "En el estudio",
        "opciones": [
            "Explicar tarea a compañero",
            "Proponer forma distinta de hacer proyecto",
            "Organizar apuntes en calendario",
            "Construir maqueta o experimento manual"
        ]
    },
    {
        "titulo": "En el trabajo",
        "opciones": [
            "Coordinar al equipo",
            "Inventar mejor método de trabajo",
            "Llevar cuentas exactas de gastos",
            "Reparar herramientas de trabajo"
        ]
    },
    {
        "titulo": "En el tiempo libre",
        "opciones": [
            "Organizar salida con amigos o barrio",
            "Imaginar historias o nuevos juegos",
            "Comparar estadísticas de equipos",
            "Arreglar o desarmar aparatos viejos"
        ]
    },
    {
        "titulo": "Resolviendo problemas",
        "opciones": [
            "Escuchar para aconsejar",
            "Pensar en solución ingeniosa",
            "Analizar hechos fríos y datos",
            "Arreglar falla técnica con herramientas"
        ]
    },
    {
        "titulo": "En un proyecto grupal",
        "opciones": [
            "Motivar al grupo para que nadie se quede atrás",
            "Diseñar concepto creativo visual",
            "Escribir informe y organizar datos",
            "Encargarse de la parte mecánica o física"
        ]
    },
    {
        "titulo": "Aprendiendo algo nuevo",
        "opciones": [
            "Aprender a mediar conflictos",
            "Aprender el 'porqué' profundo de las cosas",
            "Aprender a leer gráficos y tablas",
            "Aprender a usar una máquina o herramienta nueva"
        ]
    },
    {
        "titulo": "Tu futuro",
        "opciones": [
            "Ser alguien que guía y apoya a su comunidad",
            "Ser alguien que crea cosas nuevas",
            "Ser alguien que planea y analiza para tener éxito",
            "Ser experto en un oficio o trabajo técnico"
        ]
    }
]

def renderizar_ronda_relampago() -> str | None:
    """
    Renderiza el componente de arrastrar y soltar de la Ronda Relámpago.
    Muestra las 7 situaciones para que el estudiante ordene sus 4 opciones.
    Calcula los puntos y retorna el string de resultado al chat.
    """
    st.subheader("Tu Mapa de Intereses")
    
    nombre_estudiante = st.session_state.estudiante.get("nombre", "") if "estudiante" in st.session_state else ""
    st.write(f"{nombre_estudiante}, ordena estas situaciones del 1 al 4 (de lo que más te gusta a lo que menos).")

    if "ronda_respuestas" not in st.session_state:
        # Inicializar el estado local con las opciones predeterminadas
        st.session_state.ronda_respuestas = [list(s["opciones"]) for s in SITUACIONES_RONDA]

    for i, situacion in enumerate(SITUACIONES_RONDA):
        with st.container():
            st.write(f"#### {situacion['titulo']}")
            st.session_state.ronda_respuestas[i] = sort_items(
                st.session_state.ronda_respuestas[i],
                direction='vertical',
                key=f"ronda_sort_{i}"
            )
            st.divider()  # Añade una línea visual entre cada escenario

    st.markdown("---")
    if st.button("✅ Listo, he ordenado todas las situaciones", use_container_width=True, type="primary", key="btn_enviar_ronda"):
        # Cálculo de puntos: 1ro=4pts, 2do=3pts, 3ro=2pts, 4to=1pt
        puntos = {"Personas": 0, "Ideas": 0, "Datos": 0, "Cosas": 0}
        
        for i, respuestas in enumerate(st.session_state.ronda_respuestas):
            # Obtener las opciones originales para esta situación para saber su categoría
            opciones_originales = SITUACIONES_RONDA[i]["opciones"]
            categorias = ["Personas", "Ideas", "Datos", "Cosas"]
            
            for idx, opc in enumerate(respuestas):
                puntaje = 4 - idx
                # Encontrar el índice original de la opción para saber su categoría
                if opc in opciones_originales:
                    cat_idx = opciones_originales.index(opc)
                    categoria = categorias[cat_idx]
                    puntos[categoria] += puntaje

        # Limpiar el session_state para la próxima iteración si se vuelve a llamar
        del st.session_state["ronda_respuestas"]
        
        return f"He completado la Ronda Relámpago. Resultados: Personas=[{puntos['Personas']}], Ideas=[{puntos['Ideas']}], Datos=[{puntos['Datos']}], Cosas=[{puntos['Cosas']}]"

    return None

# ── Inicialización del chat ────────────────────────────────────────────────────
# Keyed por UUID del estudiante para aislar sesiones entre usuarios
# en la misma instancia Streamlit.
CHAT_KEY = f"chat_{estudiante['id']}"

SALUDO_INICIAL = """¡Hola! Soy rAÍz, tu mentor de proyecto de vida 🌱

Soy una inteligencia artificial — un programa diseñado para acompañarte — pero estoy aquí para escucharte de verdad. Quiero que te sientas seguro/a hablando conmigo.

Nuestra charla va a estar dividida en 4 grandes bloques donde vamos a hablar de ti: de lo que te gusta, lo que se te da bien, lo que sueñas, y cómo es tu día a día. No tienes que hacerlos todos de una vez, puedes avanzar a tu propio ritmo y pausar cuando lo necesites. Pero eso sí, es muy importante que lleguemos hasta el final; si no terminas el proceso, no tendré la información completa para poder ayudarte de verdad.

No hay respuestas buenas ni malas, y tampoco hay ningún afán. Solo te pido que vengas con la mente abierta y ganas de hablar de ti. Con eso es suficiente.

Lo importante es empezar a reflexionar juntos desde ahora. Vas a ver que de acá van a salir cosas clave que te van a servir muchísimo en 10° y 11°, y para todo lo que viene después del colegio.

Al final, lo que descubramos le va a llegar a tu profe orientador/a para que te pueda acompañar mejor — pero lo más personal se queda entre nosotros. Todo queda guardado de forma segura, y si algún día querés que borremos algo, podés pedírselo a tu orientador/a.

¿Listo/a para arrancar?"""

if CHAT_KEY not in st.session_state:
    historial_db = db.get_historial(estudiante["id"])

    if historial_db:
        # Retoma de sesión: reconstruir el contexto completo de Gemini.
        # Se pasa el historial crudo (con etiquetas internas) para que el modelo
        # mantenga coherencia sobre perfil de riesgo y avance pedagógico.
        gemini_history = [
            types.Content(
                role=msg["rol"],
                parts=[types.Part(text=msg["contenido"])],
            )
            for msg in historial_db
        ]
        st.session_state[CHAT_KEY] = client.chats.create(
            model=MODEL,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.5,
            ),
            history=gemini_history,
        )
        st.session_state.history_ui = [
            {"role": msg["rol"], "text": msg["contenido"]}
            for msg in historial_db
        ]
    else:
        # Primera sesión: chat limpio y saludo inicial guardado en DB.
        st.session_state[CHAT_KEY] = client.chats.create(
            model=MODEL,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.5,
            ),
            history=[
                types.Content(
                    role="model",
                    parts=[types.Part(text=SALUDO_INICIAL)],
                )
            ]
        )
        st.session_state.history_ui = [{"role": "model", "text": SALUDO_INICIAL}]
        db.guardar_mensaje(
            estudiante_uuid=estudiante["id"],
            sesion=estudiante.get("sesion_actual", 1),
            rol="model",
            contenido=SALUDO_INICIAL,
        )

if "fin_consejeria" not in st.session_state:
    # Inicializar desde DB por si la mentoría fue completada en una sesión anterior.
    st.session_state.fin_consejeria = bool(estudiante.get("mentoria_completada", False))

# ── Recuperación de estado de la Ronda Relámpago tras recarga ─────────────────
# NO inicializar ronda_activa = False directamente: si el navegador se recarga
# (comportamiento normal en móviles), perderíamos el estado y el flujo se rompería.
# En cambio, derivamos el estado leyendo el último mensaje del historial en DB:
# si el modelo emitió [INICIAR_RONDA_RELAMPAGO] y el estudiante aún no respondió
# (es decir, el último mensaje es del modelo), reactivamos la ronda automáticamente.
if "ronda_activa" not in st.session_state:
    historial_para_ronda = db.get_historial(estudiante["id"])
    ronda_pendiente = (
        bool(historial_para_ronda)
        and historial_para_ronda[-1]["rol"] == "model"
        and "[INICIAR_RONDA_RELAMPAGO]" in historial_para_ronda[-1]["contenido"]
    )
    st.session_state.ronda_activa = ronda_pendiente

# ── Render del historial ───────────────────────────────────────────────────────
for message in st.session_state.history_ui:
    with st.chat_message("assistant" if message["role"] == "model" else "user"):
        st.markdown(limpiar_etiquetas(message["text"]))

# ── Ronda Relámpago — intercepción y render ────────────────────────────────────
# Cuando la bandera está activa, mostramos el componente táctil y bloqueamos
# el input de texto normal hasta que el estudiante envíe su selección.
if st.session_state.ronda_activa and not st.session_state.fin_consejeria:
    resultado_ronda = renderizar_ronda_relampago()

    if resultado_ronda is not None:
        sesion_actual = st.session_state.estudiante.get("sesion_actual", 1)

        # Mostrar el mensaje del estudiante en el chat (versión legible)
        with st.chat_message("user"):
            actividades = resultado_ronda.replace("He seleccionado estas actividades: ", "")
            if actividades == "ninguna en particular":
                st.markdown("_(No seleccioné ninguna actividad en particular)_")
            else:
                st.markdown(f"Seleccioné: {actividades}")

        st.session_state.history_ui.append({"role": "user", "text": resultado_ronda})
        db.guardar_mensaje(
            estudiante_uuid=estudiante["id"],
            sesion=sesion_actual,
            rol="user",
            contenido=resultado_ronda,
        )

        # Enviar el resultado al modelo y procesar su respuesta
        with st.chat_message("assistant"):
            try:
                response = st.session_state[CHAT_KEY].send_message(resultado_ronda)
                raw = response.text
                tiene_alerta = _procesar_etiquetas(raw, estudiante, sesion_actual)
                db.guardar_mensaje(
                    estudiante_uuid=estudiante["id"],
                    sesion=sesion_actual,
                    rol="model",
                    contenido=raw,
                    tiene_alerta=tiene_alerta,
                )
                st.session_state.history_ui.append({"role": "model", "text": raw})
                st.markdown(limpiar_etiquetas(raw))
            except Exception as e:
                st.error(f"Error de conexión. Detalles: {e}")

        # Desactivar la ronda y volver al flujo normal
        st.session_state.ronda_activa = False
        st.rerun()

# ── Interacción normal ─────────────────────────────────────────────────────────
# El input se deshabilita mientras la Ronda Relámpago está activa o la
# mentoría está completada.
user_input = st.chat_input(
    "Escribe tu respuesta aquí...",
    disabled=st.session_state.ronda_activa or st.session_state.fin_consejeria,
)

if user_input and not st.session_state.fin_consejeria and not st.session_state.ronda_activa:
    sesion_actual = st.session_state.estudiante.get("sesion_actual", 1)

    with st.chat_message("user"):
        st.markdown(user_input)
    st.session_state.history_ui.append({"role": "user", "text": user_input})
    db.guardar_mensaje(
        estudiante_uuid=estudiante["id"],
        sesion=sesion_actual,
        rol="user",
        contenido=user_input,
    )

    with st.chat_message("assistant"):
        try:
            response = st.session_state[CHAT_KEY].send_message(user_input)
            raw = response.text

            # Detectar si el modelo quiere lanzar la Ronda Relámpago
            if "[INICIAR_RONDA_RELAMPAGO]" in raw:
                st.session_state.ronda_activa = True

            tiene_alerta = _procesar_etiquetas(raw, estudiante, sesion_actual)

            db.guardar_mensaje(
                estudiante_uuid=estudiante["id"],
                sesion=sesion_actual,
                rol="model",
                contenido=raw,
                tiene_alerta=tiene_alerta,
            )
            st.session_state.history_ui.append({"role": "model", "text": raw})
            st.markdown(limpiar_etiquetas(raw))

            # Si se activó la ronda, hacer rerun para renderizarla de inmediato
            if st.session_state.ronda_activa:
                st.rerun()

        except Exception as e:
            st.error(f"Error de conexión. Detalles: {e}")

# ── Pantalla de cierre ─────────────────────────────────────────────────────────
if st.session_state.fin_consejeria:
    st.success("¡Completaste tu proceso rAÍz! 🌱")
    st.markdown(
        "Tu mapa de fortalezas e intereses ya está listo. "
        "Descargalo para guardarlo y compartirlo con quien quieras."
    )

    # ── Generación del PDF: separada del botón de descarga ────────────────────
    # IMPORTANTE: st.download_button dispara un rerun de Streamlit al hacer clic.
    # Si pdf_generator.generar_pdfs() viviera dentro del mismo bloque que el
    # download_button, se llamaría a la API de Gemini en CADA clic de descarga.
    # Solución: generar una sola vez y cachear los bytes en session_state.
    # El download_button solo lee los bytes ya cacheados — sin costo adicional.

    if "pdf_estudiante_bytes" not in st.session_state:
        # Primera vez: mostrar botón para generar
        if st.button("⚙️ Generar mi Mapa rAÍz", use_container_width=True, type="primary"):
            with st.spinner("Generando tu Mapa rAÍz... esto toma unos segundos ⏳"):
                try:
                    import pdf_generator
                    pdf_est, pdf_ori = pdf_generator.generar_pdfs(
                        estudiante=st.session_state.estudiante,
                        historial=db.get_historial(st.session_state.estudiante["id"]),
                        client=client,
                        model=MODEL,
                        system_instruction=system_instruction,
                    )
                    # Cachear ambos PDFs en session_state — no se vuelve a llamar la API
                    st.session_state["pdf_estudiante_bytes"] = pdf_est
                    st.session_state["pdf_orientador_bytes"] = pdf_ori

                    # ── Enviar ficha al orientador por email (una sola vez) ────
                    try:
                        from email_service import enviar_ficha_orientador
                        sede_info = db.get_sede_info(
                            st.session_state.estudiante.get("sede_id", 0)
                        )
                        orientador_email = sede_info.get("orientador_email") if sede_info else None
                        if orientador_email:
                            nombre_est = (
                                f"{st.session_state.estudiante.get('nombre', '')} "
                                f"{st.session_state.estudiante.get('apellido', '')}"
                            ).strip()
                            exito = enviar_ficha_orientador(
                                destinatario=orientador_email,
                                nombre_estudiante=nombre_est,
                                pdf_bytes=pdf_ori,
                            )
                            db.registrar_envio_ficha(
                                estudiante_id=st.session_state.estudiante["id"],
                                orientador_email=orientador_email,
                                exito=exito,
                            )
                    except Exception:
                        # El envío fallido no interrumpe la experiencia del estudiante.
                        # La ficha queda en DB; el orientador la verá desde el dashboard.
                        try:
                            db.registrar_envio_ficha(
                                estudiante_id=st.session_state.estudiante["id"],
                                orientador_email="desconocido",
                                exito=False,
                            )
                        except Exception:
                            pass

                    # ── Enviar el PDF del estudiante al propio estudiante ──
                    est_email = st.session_state.estudiante.get("email")
                    est_celular = st.session_state.estudiante.get("celular")
                    nombre_est_completo = f"{st.session_state.estudiante.get('nombre', '')} {st.session_state.estudiante.get('apellido', '')}".strip()

                    enviado_por_correo = False
                    if est_email:
                        try:
                            from email_service import enviar_mapa_estudiante
                            enviado_por_correo = enviar_mapa_estudiante(est_email, nombre_est_completo, pdf_est)
                        except Exception:
                            pass
                    
                    # Priorizar correo: evitar costos de Meta/Twilio si ya se envió el PDF.
                    # Fallback a WhatsApp solo si no tiene correo o si el envío por correo falló.
                    if est_celular and not enviado_por_correo:
                        try:
                            from whatsapp_service import enviar_mapa_estudiante as enviar_mapa_wa
                            url_pdf = db.subir_pdf_temporal_whatsapp(pdf_est)
                            if url_pdf:
                                enviar_mapa_wa(est_celular, nombre_est_completo, url_pdf)
                        except Exception:
                            pass

                    st.rerun()  # Rerun para mostrar el botón de descarga

                except Exception as e:
                    st.error(
                        f"Hubo un problema generando el PDF. "
                        f"Intentá de nuevo o pedile ayuda a tu orientador/a. ({e})"
                    )
    else:
        # PDF ya generado: solo mostrar botón de descarga (sin costo de API)
        nombre = st.session_state.estudiante.get("nombre", "estudiante").lower().replace(" ", "_")
        st.success("¡Tu Mapa rAÍz está listo! Descárgalo abajo 👇")
        st.download_button(
            label="📄 Descargar Mi Mapa rAÍz (PDF)",
            data=st.session_state["pdf_estudiante_bytes"],
            file_name=f"mapa_raiz_{nombre}.pdf",
            mime="application/pdf",
            use_container_width=True,
        )
