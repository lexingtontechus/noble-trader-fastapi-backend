   TimesFM Forecasting API - Swagger UI

TimesFM Forecasting API 1.1.0 

OAS 3.1
=======================================

[/openapi.json](/openapi.json)

Authorize

### [default](#/default)

GET

[/](#/default/root__get)

Root

GET

[/health](#/default/health_health_get)

Health

GET

[/info](#/default/info_info_get)

Info

#### Parameters

Try it out

No parameters

#### Responses

Code

Description

Links

200

Successful Response

Media type

application/json

Controls `Accept` header.

*   Example Value
*   Schema

    "string"

_No links_

POST

[/forecast](#/default/forecast_forecast_post)

Forecast

#### Parameters

Try it out

No parameters

#### Request body

application/json

*   Example Value
*   Schema

    {
      "horizon": 1,
      "inputs": [
        [
          0
        ]
      ]
    }

#### Responses

Code

Description

Links

200

Successful Response

Media type

application/json

Controls `Accept` header.

*   Example Value
*   Schema

    {
      "horizon": 0,
      "num_series": 0,
      "point_forecast": [
        [
          0
        ]
      ],
      "quantile_forecast": [
        [
          [
            0
          ]
        ]
      ],
      "elapsed_ms": 0
    }

_No links_

422

Validation Error

Media type

application/json

*   Example Value
*   Schema

    {
      "detail": [
        {
          "loc": [
            "string",
            0
          ],
          "msg": "string",
          "type": "string",
          "input": "string",
          "ctx": {}
        }
      ]
    }

_No links_

#### Schemas

ForecastRequest

Expand all**object**

ForecastResponse

Expand all**object**

HTTPValidationError

Expand all**object**

ValidationError

Expand all**object**

const ui = SwaggerUIBundle({ url: '/openapi.json', "dom\_id": "#swagger-ui", "layout": "BaseLayout", "deepLinking": true, "showExtensions": true, "showCommonExtensions": true, oauth2RedirectUrl: window.location.origin + '/docs/oauth2-redirect', presets: \[ SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset \], })
