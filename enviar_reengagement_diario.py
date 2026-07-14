import database
import whatsapp_service

def main():
    print("Iniciando envío de re-engagement automático...")
    try:
        resultado = whatsapp_service.procesar_reengagement(database)
        print(f"Proceso completado con éxito: {resultado['enviados']} enviados, {resultado['fallidos']} fallidos de {resultado['total']} candidatos.")
    except Exception as e:
        print(f"Error durante el envío de re-engagement: {e}")

if __name__ == "__main__":
    main()
