# SISPRO (RIPS sin FEV)

Automatiza el envío secuencial de `RIPS sin factura` en Colombia contra la API local de SISPRO usando Docker y un script de Python.

## Compatibilidad

- Validado en macOS con OrbStack y Docker Desktop
- Debería funcionar en Windows con Docker Desktop en modo de contenedores Linux y Python 3

La imagen principal de SISPRO está fijada a `linux/amd64`, por lo que Apple Silicon y algunos equipos Windows dependen de la emulación x86_64 del motor Docker.

## Estructura

```text
sispro-rips_sin_fev/
  .env
  .env.example
  LICENSE
  send_rips_sin_factura.py
  runtime/
    docker-compose.yml
    fevrips_proxy.py
    certificates/
  batches/
    YYYY-MM/
      <carpeta-reporte>/
        <reporte>.json
        <reporte>.result.json
```

Las carpetas `batches/`, `.env` y `runtime/certificates/` son locales y no deben versionarse con datos reales.

## Requisitos

- Python 3.11 o superior
- Docker con soporte para contenedores Linux
- OpenSSL
- Credenciales SISPRO válidas para `RIPS sin factura`

## Configuración

1. Crear el archivo local de entorno:

```bash
cp .env.example .env
```

2. Completar en `.env` los valores reales de SISPRO.

## Certificados desde cero

Crear la carpeta de certificados:

```bash
mkdir -p runtime/certificates
```

Generar la llave privada:

```bash
openssl genrsa -out runtime/certificates/fevripsapilocal.key 2048
```

Generar el certificado autofirmado para `localhost`:

```bash
openssl req -x509 -new \
  -key runtime/certificates/fevripsapilocal.key \
  -sha256 -days 3650 \
  -out runtime/certificates/fevripsapilocal.crt \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost"
```

Generar el `.pfx` que consume la API Docker:

```bash
openssl pkcs12 -export \
  -out runtime/certificates/fevripsapilocal.pfx \
  -inkey runtime/certificates/fevripsapilocal.key \
  -in runtime/certificates/fevripsapilocal.crt \
  -passout pass:fevrips2024*
```

Si usas otra contraseña para el `.pfx`, define `FEVRIPS_CERT_PASSWORD` en `.env` con el mismo valor.

## Uso

macOS / Linux:

```bash
./send_rips_sin_factura.py /ruta/al/lote-mensual
```

Ejemplo:

```bash
./send_rips_sin_factura.py batches/2001-01
```

Windows:

```powershell
python .\send_rips_sin_factura.py batches\2001-01
```

Comportamiento por defecto:

1. Carga `.env`
2. Ejecuta `docker compose up -d`
3. Inicia sesión en la API local
4. Envía cada JSON pendiente en secuencia
5. Imprime el resultado en terminal
6. Guarda un sidecar `<reporte>.result.json` junto al reporte original
7. Omite reportes que ya tengan sidecar
8. Hace una pausa entre reportes cuando la terminal es interactiva

## Opciones útiles

```bash
./send_rips_sin_factura.py batches/2001-01 --force
./send_rips_sin_factura.py batches/2001-01 --no-compose
./send_rips_sin_factura.py batches/2001-01 --verify-tls
```

- `--force`: reenvía reportes aunque ya exista su sidecar
- `--no-compose`: no levanta Docker antes del envío
- `--verify-tls`: valida TLS en lugar de omitir la verificación del certificado local

## Verificación rápida

```bash
python3 -m unittest test_runtime_behaviors.py
```

Valida la lógica local de caché, reintentos y nombres de sidecar. No realiza envíos a SISPRO; también comprueba que el cliente Docker actual puede hablar con un daemon.

## Variables de entorno

Requeridas:

- `SISPRO_ID_NUMBER`
- `SISPRO_PASSWORD`
- `SISPRO_ID_TYPE`
- `SISPRO_NIT`

Habitual en este flujo:

- `SISPRO_TIPO_USUARIO=PIN`

Opcionales:

- `SISPRO_TIPO_MECANISMO_VALIDACION`
- `SISPRO_REPS`
- `FEVRIPS_BASE_URL`
- `FEVRIPS_COMPOSE_FILE`
- `FEVRIPS_PAUSE_BETWEEN`
- `FEVRIPS_CERT_PASSWORD`

Todas estas variables se leen desde `.env`. El script no acepta banderas que las sobrescriban.

Variables avanzadas de timeout, reintento y caché existen como overrides opcionales, pero no aparecen en `.env.example` porque el flujo normal puede usar los valores internos del código:

- `FEVRIPS_HTTP_TIMEOUT_SECONDS`
- `FEVRIPS_API_READY_TIMEOUT_SECONDS`
- `FEVRIPS_API_READY_RETRY_INTERVAL_SECONDS`
- `FEVRIPS_SEND_RETRY_ATTEMPTS`
- `FEVRIPS_SEND_RETRY_INTERVAL_SECONDS`
- `FEVRIPS_PROXY_TIMEOUT`
- `FEVRIPS_PROXY_DISABLE_CACHE`
- `FEVRIPS_PROXY_TABLE_CACHE_TTL_SECONDS`
- `FEVRIPS_PROXY_METADATA_CACHE_TTL_SECONDS`
