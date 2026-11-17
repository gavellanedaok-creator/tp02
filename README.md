# TP-2 — Vulcano Pay

¡Bienvenido/a! Este proyecto recibe webhooks de pagos, valida su autenticidad y
publica los eventos en AWS SQS para procesarlos de forma asíncrona.

La integración local utiliza `moto_server` en `http://localhost:4566`. **No hace
falta instalar Docker ni configurar credenciales reales de AWS.**

## Antes de empezar

- Windows y PowerShell (por ejemplo, la terminal integrada de VS Code).
- Python **3.11 o superior**.
- `moto_server`, que se instala junto con las dependencias del proyecto.

### ¿Por qué falló `py -3.11`?

Ese mensaje significa que el launcher de Windows no encuentra Python 3.11
instalado. No es un error del proyecto. **Python 3.12 también cumple el
requisito** y puede usarse para crear el entorno virtual.

Puedes ver las versiones que Windows detecta con:

```powershell
py -0p
```

En los pasos de abajo se usa Python 3.12. Si tienes otra versión 3.11 o superior,
reemplaza `3.12` por esa versión. Si `py -3.12` tampoco funciona, consulta la
ruta de `python.exe` que muestra `py -0p` y úsala directamente, por ejemplo:

```powershell
& "C:\Users\TU_USUARIO\AppData\Local\Programs\Python\Python312\python.exe" -m venv .venv
```

## Instalación

Abre PowerShell en la carpeta que contiene `app_operator.py` y
`requirements.txt`. En VS Code puedes abrir esa carpeta y seleccionar
**Terminal → Nuevo terminal**.

Ejecuta estos comandos:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`python --version` debe mostrar Python 3.11 o superior. Si PowerShell bloquea la
activación del entorno, permite scripts locales solo durante esta sesión y
vuelve a activarlo:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

Cuando el entorno esté activo, el prompt suele comenzar con `(.venv)`.

## Cómo ejecutar el proyecto

Se necesitan **tres terminales**. Mantén activo `.venv` en cada terminal; si
abres una nueva, activa el entorno otra vez con
`.\.venv\Scripts\Activate.ps1`.

### 1. Iniciar SQS local

En la primera terminal:

```powershell
moto_server -H 127.0.0.1 -p 4566
```

Déjala abierta. El servicio local queda disponible en `http://localhost:4566`.

### 2. Iniciar el receptor HTTP

En la segunda terminal, configura un secreto de desarrollo y ejecuta el
servidor:

```powershell
$env:VULCANO_WEBHOOK_SECRET = "secreto-local-de-desarrollo"
python .\app_operator.py server `
  --queue-name vulcano-payments-q `
  --endpoint-url http://localhost:4566 `
  --host 127.0.0.1 `
  --port 8000
```

El endpoint queda en `http://127.0.0.1:8000/webhook`. Al arrancar, el programa
crea la cola si todavía no existe. La base local de protección anti-replay se
guarda en `vulcano_replay.sqlite3`; puedes cambiarla con `--replay-database`.

### 3. Enviar un webhook de prueba

En una tercera terminal, vuelve a configurar el mismo secreto —las variables
de entorno de PowerShell son propias de cada terminal— y envía el evento:

```powershell
$env:VULCANO_WEBHOOK_SECRET = "secreto-local-de-desarrollo"
python .\app_operator.py emit-webhook `
  --url http://127.0.0.1:8000/webhook
```

El comando genera un `event_id`, firma el cuerpo y espera la respuesta HTTP
`202 Accepted`.

### 4. Leer los eventos de SQS

Puedes usar una cuarta terminal, o detener el emisor y reutilizar la tercera:

```powershell
python .\app_operator.py worker `
  --queue-name vulcano-payments-q `
  --endpoint-url http://localhost:4566 `
  --wait-time 20
```

El worker ejecuta long-polling continuo, muestra cada mensaje y lo borra de la
cola después de leerlo. Para leer un único lote y salir, agrega `--once`.
`--max-messages` permite elegir entre 1 y 10 mensajes por lote.

## Qué implementa

- **Webhook HTTP:** FastAPI `POST /webhook`; devuelve `202` cuando el evento se
  publicó correctamente en SQS.
- **Validación criptográfica:** HMAC-SHA256 y comparación en tiempo constante
  mediante `hmac.compare_digest`.
- **Ventana temporal:** timestamp Unix aceptado con tolerancia predeterminada
  de 300 segundos.
- **Anti-replay:** el `event_id` firmado se reclama atómicamente en SQLite y no
  puede aceptarse de nuevo dentro de la ventana válida.
- **SQS:** crear cola, publicar, recibir con long-polling y borrar eventos.
- **Validaciones de configuración:** nombre de cola con formato
  `vulcano-[a-z0-9]+-q` y `wait_time` de 1 a 20 segundos.
- **Sanitización:** redacta valores de campos sensibles antes de encolar el
  evento.
- **CLI:** subcomandos `server`, `emit-webhook` y `worker`.

La firma se envía en `X-Vulcano-Signature`, junto con
`X-Vulcano-Timestamp` y `X-Vulcano-Event-Id`. Se calcula sobre los bytes:

```text
timestamp + "." + event_id + "." + cuerpo HTTP original
```

El secreto se proporciona con `VULCANO_WEBHOOK_SECRET` o `--secret`. Usa un
secreto de desarrollo local; no compartas ni subas secretos reales al
repositorio.

## Ejecutar las pruebas

Desde la carpeta del proyecto y con `.venv` activo:

```powershell
python -m pytest -q
```

Las pruebas validan firmas válidas e inválidas, timestamp expirado,
anti-replay, sanitización, validaciones de cola/espera y operaciones del
adaptador SQS. Usan un cliente SQS simulado, por lo que no requieren que
`moto_server` esté activo.

## Archivos principales

```text
TP02/
├── README.md
├── requirements.txt
├── app_operator.py
├── src/
│   └── vulcano_telemetry/
│       ├── __init__.py
│       ├── exceptions.py
│       ├── sanitizer.py
│       ├── webhook_crypto.py
│       ├── core_sqs.py
│       └── receiver.py
└── tests/
    └── test_caos.py
```

La consigna compartida describe un receptor de webhooks que publique eventos
asíncronamente en un broker Pub/Sub (RabbitMQ o AWS SQS). Esta implementación
elige **AWS SQS**. El texto adicional recibido para “levantar nota” estaba
incompleto, así que esta guía documenta los requisitos visibles sin inventar
criterios que no fueron compartidos.
