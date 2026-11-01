# Mantenimiento de Carros

App web para llevar el control de tus carros y avisarte cuándo toca cada servicio:
cambio de aceite, verificación, seguro, tenencia, llantas, frenos, etc.

- Agregas todos tus carros (marca, modelo, placa, kilometraje)
- Cada servicio se controla por **kilómetros**, por **meses** o por **fecha límite fija**
- La **verificación** se calcula sola con el calendario de CDMX / Edomex según el último dígito de la placa (o el color del engomado)
- Pantalla de "Lo que toca pronto": vencidos y lo que vence en los próximos 30 días o 1,000 km
- Historial de cada servicio con costo y taller
- Botón **📅 Calendario**: descarga los recordatorios para tu Google Calendar / iPhone, con aviso una semana antes
- Los datos se guardan en **Cloudflare R2** (y se hace un respaldo diario en `respaldos/`)
- Protegida con un PIN

## Cómo ponerla en internet (una sola vez)

Necesitas una cuenta de Cloudflare (gratis) y Node.js instalado.

```bash
cd mantenimiento-carros
npm install

# 1. Inicia sesión en Cloudflare (abre el navegador)
npx wrangler login

# 2. Crea el bucket de R2 donde se guardan los datos
#    (si es la primera vez que usas R2, actívalo en el dashboard: R2 → "Purchase R2 Plan"; el plan gratis no cobra)
npx wrangler r2 bucket create mantenimiento-carros

# 3. Elige tu PIN para entrar a la app
npx wrangler secret put APP_PIN

# 4. Publica
npx wrangler deploy
```

Al terminar te da una dirección tipo `https://mantenimiento-carros.TU-USUARIO.workers.dev`.
Ábrela en el celular y "Agregar a pantalla de inicio" para usarla como app.

## Probar en tu compu

```bash
echo "APP_PIN=1234" > .dev.vars
npm run dev          # abre http://localhost:8787
```
