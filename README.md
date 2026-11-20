# TP-2 — Vulcano Pay

¡Bienvenido/a! Este proyecto recibe webhooks de pagos, valida su autenticidad y
publica los eventos en AWS SQS para procesarlos de forma asíncrona.

La integración local utiliza `moto_server` en `http://localhost:4566`. **No hace
falta instalar Docker ni configurar credenciales reales de AWS.**

## Prerrequisitos

- Windows con PowerShell (por ejemplo, la terminal integrada de VS Code).
- Python **3.11 o superior**, disponible desde la terminal.
- Git, para clonar el repositorio.
- Conexión a Internet para instalar las dependencias con `pip`.
- VS Code es opcional; también se puede trabajar desde PowerShell.

No se necesita Docker ni una cuenta o credenciales reales de AWS. `moto_server`,
que simula SQS localmente, se instala junto con las dependencias del proyecto.

## Clonar el repositorio

Abre PowerShell en la carpeta donde quieras guardar el proyecto y ejecuta:

```powershell
git clone https://github.com/gavellanedaok-creator/tp02.git
cd tp02
```

Los siguientes comandos se ejecutan desde la carpeta `tp02`, donde están
`app_operator.py` y `requirements.txt`.

### Distribución de responsabilidades

| #     | Apellido y Nombre      | Rol Técnico                                      | Módulos bajo su firma                 | Batería de Pruebas a Defender                                                                                                    |
| ----- | ---------------------- | ------------------------------------------------ | ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| **1** | **Gabriel Avellaneda** | Ingeniero de Robustez, Entradas y Excepciones    | `exceptions.py` `sanitizer.py`        | Validación de nombres de colas, rangos numéricos de *wait time*, sanitización y errores de dominio.                              |
| **2** | **Hernán Lamadrid**    | Ingeniero de Mensajería y Broker Cloud SQS       | `core_sqs.py`                         | Creación de colas, publicación con atributo `event_id`, consumo *long-polling* y borrado de mensajes (ACK).                       |
| **3** | **Lorenzo Lamas**      | Ingeniero de Criptografía y Receptor Webhook     | `webhook_crypto.py` `receiver.py`     | Firmas HMAC SHA-256 en tiempo constante (`compare_digest`), mitigación de *replay attacks* (ventana 300s) y respuesta HTTP 202.  |
| **4** | **Maximiliano Ríos**   | Coordinador de Integración y CLI                | `app_operator.py`                      | Comandos `server`, `emit-webhook` y `worker`, y demostración del circuito completo.                                               |

---

### **Detalle de Responsabilidades por Integrante**

#### **1\. Gabriel Avellaneda — Robustez de Entradas y Excepciones**

* **Módulos:** `src/vulcano_telemetry/exceptions.py` y `src/vulcano_telemetry/sanitizer.py`.
* **Alcance técnico:**
  * Definir errores del dominio que heredan de `Exception`, como `InvalidQueueNameError`, `InvalidWaitTimeError`, `InvalidSignatureError` y `ReplayAttackError`.
  * Validar nombres de colas con `^vulcano-[a-z0-9]+-q$` y `WaitTimeSeconds` en el rango de 1 a 20 segundos.
  * Sanitizar recursivamente campos sensibles antes de encolar los eventos.
* **Prueba para ejecutar:** `python -m pytest -q tests/test_caos.py -k sanitization_and_queue_parameter_validation`.
* **Resultado esperado:** una prueba aprobada; se aceptan `vulcano-payments-q` y el tiempo 20, se rechazan `payments`, 0 y 21, y los datos sensibles aparecen como `[REDACTED]`.
* **Defensa en video:** Muestra las validaciones de nombre, espera y sanitización, y explica qué excepción corresponde a cada entrada inválida.

---

#### **2\. Hernán Lamadrid — Mensajería y Broker SQS**

* **Módulo:** `src/vulcano_telemetry/core_sqs.py`.
* **Alcance técnico:**
  * Conectar `boto3` con el endpoint local `http://localhost:4566` en la región `us-east-1`.
  * Crear la cola, enviar eventos con el atributo `event_id`, recibirlos con *long-polling* y borrarlos después de procesarlos.
  * Traducir errores de operación del SDK a errores del dominio SQS.
* **Prueba para ejecutar:** `python -m pytest -q tests/test_caos.py -k sqs_client_creates_sends_receives_and_deletes`.
* **Resultado esperado:** una prueba aprobada; verifica las cuatro operaciones, el atributo `event_id`, la espera de 7 segundos y el borrado con el *receipt handle*. Esta prueba usa un cliente simulado, no requiere iniciar `moto_server`.
* **Defensa en video:** Muestra las llamadas capturadas por el cliente simulado y explica el flujo de creación, envío, recepción y borrado.

---

#### **3\. Lorenzo Lamas — Seguridad Criptográfica y Endpoint Receptor**

* **Módulos:** `src/vulcano_telemetry/webhook_crypto.py` y `src/vulcano_telemetry/receiver.py`.
* **Alcance técnico:**
  * Calcular y validar firmas HMAC-SHA256 mediante `hmac.compare_digest`.
  * Rechazar timestamps con más de 300 segundos de diferencia y evitar aceptar dos veces el mismo `event_id`.
  * Validar `POST /webhook`, sanitizar el evento y responder `202 Accepted` cuando se encola correctamente.
* **Pruebas para ejecutar:** `python -m pytest -q tests/test_caos.py -k "signature or timestamp or duplicate_event or future_timestamp or replay"`.
* **Resultado esperado:** seis pruebas aprobadas; cubren firma válida y adulterada, timestamp expirado, repetición de evento y repetición concurrente en SQLite.
* **Defensa en video:** Explica los resultados y, con el servidor iniciado, abre otra terminal, configura el mismo secreto local y ejecuta dos veces el mismo evento:

  ```powershell
  $env:VULCANO_WEBHOOK_SECRET = "secreto-local-de-desarrollo"
  python .\app_operator.py emit-webhook --url http://127.0.0.1:8000/webhook --event-id prueba-lorenzo
  python .\app_operator.py emit-webhook --url http://127.0.0.1:8000/webhook --event-id prueba-lorenzo
  ```

  La primera petición debe responder 202 y la repetición 409.

---

#### **4\. Maximiliano Ríos — Integración y CLI**

* **Módulo:** `app_operator.py`.
* **Alcance técnico:**
  * Estructurar el CLI con `argparse`: `server` para FastAPI/Uvicorn, `emit-webhook` para enviar una petición firmada y `worker` para consumir eventos.
  * Integrar y demostrar el flujo entre API, SQS local y worker.
* **Pruebas para ejecutar:** primero `python .\app_operator.py --help`; después, realizar la prueba integral de las secciones «Cómo ejecutar el proyecto» y «Ejecutar las pruebas».
* **Resultado esperado:** la ayuda enumera los tres subcomandos; el emisor informa `HTTP 202` y el worker imprime el evento recibido. El conjunto automatizado termina con ocho pruebas aprobadas.
* **Defensa en video:** Muestra los tres comandos en ejecución y sigue el mismo `event_id` desde el envío hasta la salida del worker.

---

### **Pautas Clave para el Video de Defensa**

* **Tiempo:** Cada integrante debe exponer un máximo de **7 minutos**, sumando un total que no exceda los **35 minutos grupales**[8].
* **Formato:** No es obligatorio encender la cámara web; basta con compartir la pantalla, mostrar el código bajo su firma y ejecutar la prueba o comando correspondiente en la terminal.
* **Entrega:** El video se sube a YouTube (en oculto o público) o a Google Drive (con permisos abiertos) y se coloca el enlace en la sección correspondiente del `README.md`[19].


## Instalación

Desde PowerShell, en la carpeta del repositorio que contiene `app_operator.py`
y `requirements.txt`, ejecuta:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Comprueba que `python --version` muestre Python 3.11 o superior. Si PowerShell
bloquea la activación del entorno, permite scripts locales solo durante esta
sesión y vuelve a activarlo:

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
