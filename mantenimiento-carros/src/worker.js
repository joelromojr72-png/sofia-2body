// Worker de Mantenimiento de Carros
// Guarda todos los carros, servicios e historial en un solo objeto de R2: datos.json
//
// API:
//   GET  /api/datos   -> devuelve los datos (y su ETag)
//   PUT  /api/datos   -> guarda los datos. Manda "If-Match: <etag>" para no pisar
//                        cambios hechos desde otro dispositivo (responde 409 si pasa).
// Todas las rutas piden el header "X-Pin" igual al secreto APP_PIN.

const CLAVE = "datos.json";
const VACIO = { version: 1, carros: [] };

function json(cuerpo, status = 200, extra = {}) {
  return new Response(JSON.stringify(cuerpo), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...extra },
  });
}

// Comparación en tiempo constante para que el PIN no se pueda adivinar midiendo tiempos
function iguales(a, b) {
  const ea = new TextEncoder().encode(a);
  const eb = new TextEncoder().encode(b);
  let dif = ea.length ^ eb.length;
  for (let i = 0; i < Math.max(ea.length, eb.length); i++) dif |= (ea[i] ?? 0) ^ (eb[i] ?? 0);
  return dif === 0;
}

function validar(datos) {
  return datos && typeof datos === "object" && Array.isArray(datos.carros);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (!url.pathname.startsWith("/api/")) {
      return env.ASSETS.fetch(request);
    }

    if (!env.APP_PIN) {
      return json({ error: "Falta configurar el secreto APP_PIN (npx wrangler secret put APP_PIN)" }, 500);
    }
    if (!iguales(request.headers.get("X-Pin") || "", env.APP_PIN)) {
      return json({ error: "PIN incorrecto" }, 401);
    }

    if (url.pathname !== "/api/datos") {
      return json({ error: "Ruta no encontrada" }, 404);
    }

    if (request.method === "GET") {
      const obj = await env.DATOS.get(CLAVE);
      if (!obj) return json(VACIO, 200, { ETag: '"nuevo"' });
      return new Response(obj.body, {
        headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ETag: obj.httpEtag },
      });
    }

    if (request.method === "PUT") {
      let datos;
      try {
        datos = await request.json();
      } catch {
        return json({ error: "JSON inválido" }, 400);
      }
      if (!validar(datos)) return json({ error: "Formato de datos inválido" }, 400);

      const texto = JSON.stringify(datos);
      if (texto.length > 5_000_000) return json({ error: "Los datos son demasiado grandes" }, 413);

      // Control de concurrencia: solo se guarda si nadie cambió los datos desde que los leíste
      const ifMatch = request.headers.get("If-Match");
      const opciones = { httpMetadata: { contentType: "application/json" } };
      if (ifMatch && ifMatch !== '"nuevo"') {
        opciones.onlyIf = { etagMatches: ifMatch.replaceAll('"', "") };
      } else if (ifMatch === '"nuevo"') {
        const existe = await env.DATOS.head(CLAVE);
        if (existe) return json({ error: "Los datos cambiaron en otro dispositivo" }, 409);
      }

      const guardado = await env.DATOS.put(CLAVE, texto, opciones);
      if (!guardado) return json({ error: "Los datos cambiaron en otro dispositivo" }, 409);

      // Copia de respaldo diaria, por si algún día se borra algo sin querer
      const hoy = new Date().toISOString().slice(0, 10);
      await env.DATOS.put(`respaldos/${hoy}.json`, texto, { httpMetadata: { contentType: "application/json" } });

      return json({ ok: true }, 200, { ETag: guardado.httpEtag });
    }

    return json({ error: "Método no permitido" }, 405);
  },
};
