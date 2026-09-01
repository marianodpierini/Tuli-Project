# API Invoices - Guia breve de autenticacion para equipos consumidores

## 1) Objetivo
Este documento define, en forma simple, como un equipo consumidor debe autenticarse para invocar la API de Invoices.

## 2) Esquema de autenticacion (resumen)
- Tipo: Bearer Token (OAuth 2.0 Client Credentials)
- Proveedor de tokens: Amazon Cognito
- Formato del token: JWT firmado (RS256)
- Envio del token: Header HTTP `Authorization`

## 3) Que debe hacer el equipo consumidor (Cuenta B)
1. Guardar en forma segura su `client_id` y `client_secret` (recomendado: AWS Secrets Manager).
2. Solicitar un `access_token` a Cognito usando `grant_type=client_credentials`.
3. Pedir el `scope` autorizado para su integracion.
4. Reutilizar el token hasta su expiracion y renovarlo cuando corresponda.
5. Incluir el token en cada request a la API.

## 4) Donde se envia el token
En cada llamada a la API, enviar el header:

```http
Authorization: Bearer <access_token>
```

Base URL de API (prod):

```text
https://1jbq8gyj0a.execute-api.us-east-1.amazonaws.com/prod
```

Ejemplo de endpoint:

```text
GET /invoices/send_invoices/LISTO_PARA_CARGAR
```

## 5) Ejemplo minimo de consumo
### 5.1 Obtener token

```bash
curl -X POST "https://invoice-api-auth.auth.us-east-1.amazoncognito.com/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -u "<client_id>:<client_secret>" \
  -d "grant_type=client_credentials&scope=invoice-api/invoice.read"
```

### 5.2 Llamar API con Bearer token

```bash
curl -X GET "https://1jbq8gyj0a.execute-api.us-east-1.amazonaws.com/prod/invoices/send_invoices/LISTO_PARA_CARGAR" \
  -H "Authorization: Bearer <access_token>"
```

## 6) Contrato de integracion (autenticacion)
### Responsabilidades del equipo proveedor (Cuenta A)
- Mantener disponible el endpoint de API y el mecanismo de validacion del token.
- Informar `scope` requerido por cada operacion expuesta.
- Entregar a cada consumidor su `client_id` y `client_secret`.

### Responsabilidades del equipo consumidor (Cuenta B)
- No compartir credenciales ni exponer secretos en codigo/logs.
- Usar solo los scopes asignados a su integracion.
- Enviar siempre el header `Authorization: Bearer <token>`.
- Implementar renovacion de token y manejo de errores 401/403.

## 7) Errores esperables
- `401 Unauthorized`: token ausente, invalido, expirado o con issuer/client no permitido.
- `403 Forbidden` (si aplica por politica): token valido pero sin permisos suficientes.

## 8) Datos que debe recibir el equipo consumidor
- `client_id`
- `client_secret`
- URL de token de Cognito
- Scope(s) habilitados
- Base URL de API y lista de endpoints habilitados

## 9) Vigencia
Este contrato aplica para la integracion actual de la API de Invoices en ambiente productivo, hasta nueva version del esquema de autenticacion.

## 10) Contrato funcional - GET /invoices/send_invoices/{estado}
Este endpoint devuelve facturas filtradas por estado de procesamiento.

### Metodo y ruta
- Metodo: GET
- Ruta: /invoices/send_invoices/{estado}
- Requiere autenticacion Bearer token

### Parametros
#### Parametros obligatorios
- estado (path): estado de procesamiento a consultar.

Estados esperados (referencia):
- RECIBIDO
- LISTO_PARA_CARGAR
- LOADED_BY_IT
- LOAD_FAILED
- DUPLICADO
- DESCARTADO
- EN_REVISION
- RECHAZADA
- ERROR

#### Parametros opcionales
- page (query, entero >= 1): numero de pagina.
- limit (query, entero >= 1): cantidad por pagina. Maximo 200.

Notas de paginacion:
- Si no se envia page ni limit, la API responde sin bloque pagination.
- Si se envia page o limit, se activa paginacion.
- Defaults cuando hay paginacion activa: page=1, limit=50.

### Ejemplos de request
Sin paginacion:

```text
GET /invoices/send_invoices/LISTO_PARA_CARGAR
```

Con paginacion:

```text
GET /invoices/send_invoices/LISTO_PARA_CARGAR?page=1&limit=50
```

### Respuesta exitosa (200)
La respuesta siempre incluye items. El bloque pagination solo aparece si se envio page o limit.

```json
{
  "items": [
    {
      "id_factura": 534,
      "cuit": "33-54799242-9",
      "operador": {
        "operator_aptour_id": 2571,
        "razon_social": "ASSIST-CARD ARGENTINA S.A."
      },
      "invoice_kind": "factura",
      "numero_factura": "0001-00001234",
      "branch": "0001",
      "number": "00001234",
      "voucher": "FA",
      "invoice_date": "2026-08-13",
      "month": 8,
      "year": 2026,
      "currency": "USD",
      "cotization": 1515,
      "total": 166.25,
      "cost_center_one": "Aero B",
      "cost_center_two": "Tours",
      "invoice_amount_attributes": {
        "exempt": 165.75,
        "not_computable": 0.00,
        "taxable_21": 0.00,
        "taxable_10_5": 0.00,
        "iva_perception": 0.00
      },
      "invoice_perceptions_attributes": {
        "amount": 0.50,
        "province_id": 1
      },
      "reservas": [
        {
          "reserve_id": 280598,
          "importe": 165.75
        },
        {
          "reserve_id": 280598,
          "importe": 165.75
        }
      ]
    }
  ],
  "pagination": {
    "page": 1,
    "limit": 50,
    "total_items": 120,
    "total_pages": 3,
    "has_next": true,
    "has_previous": false
  }
}
```

### Errores funcionales esperables
- 400 Bad Request: page o limit invalidos (no enteros o menores a 1).
- 500 Internal Server Error: error interno al consultar facturas.
