# app/dashboard.py
import logging
from aiohttp import web

class Dashboard:
    def __init__(self, db):
        self.db = db

    async def page(self, request: web.Request):
        """Dashboard web básico del streamer."""
        try:
            slug = request.match_info.get("slug")
            token = request.rel_url.query.get("token")

            if not slug or not token:
                return web.Response(text="❌ Parámetros inválidos.", status=400)

            b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
            if not b or b.get("dashboard_token") != token:
                return web.Response(text="❌ Acceso no autorizado.", status=403)

            try:
                logs = await self.db.all("""
                    SELECT etiqueta_semana, total_miembros, expulsados, sin_vinculo, fallos, fecha_auditoria
                    FROM audit_logs
                    WHERE broadcaster_id=%s
                    ORDER BY fecha_auditoria DESC
                    LIMIT 20
                """, b["broadcaster_id"])
            except Exception as e:
                logging.error(f"Error al obtener logs del dashboard: {e}")
                logs = []

            rows_html = ""
            if logs:
                for log in logs:
                    rows_html += f"""
                    <tr>
                        <td>{log.get('etiqueta_semana', '-')}</td>
                        <td>{log.get('total_miembros', '-')}</td>
                        <td>{log.get('expulsados', '-')}</td>
                        <td>{log.get('sin_vinculo', '-')}</td>
                        <td>{log.get('fallos', '-')}</td>
                        <td>{log.get('fecha_auditoria', '-')}</td>
                    </tr>
                    """
            else:
                rows_html = """<tr><td colspan="6" style="text-align:center;">No hay auditorías registradas aún.</td></tr>"""

            html = f"""
            <html>
            <head>
                <meta charset="utf-8">
                <title>Dashboard de {slug}</title>
                <style>
                    body {{
                        font-family: 'Segoe UI', sans-serif;
                        background-color: #0e0e10;
                        color: #f0f0f0;
                        padding: 20px;
                    }}
                    h1 {{
                        color: #9146FF;
                    }}
                    table {{
                        width: 100%;
                        border-collapse: collapse;
                        margin-top: 20px;
                        background-color: #18181b;
                        border-radius: 10px;
                        overflow: hidden;
                    }}
                    th, td {{
                        border: 1px solid #303030;
                        padding: 10px;
                        text-align: center;
                    }}
                    th {{
                        background-color: #9146FF;
                        color: white;
                    }}
                    tr:nth-child(even) {{
                        background-color: #1f1f23;
                    }}
                    .footer {{
                        margin-top: 40px;
                        text-align: center;
                        font-size: 12px;
                        color: #aaa;
                    }}
                </style>
            </head>
            <body>
                <h1>📊 Dashboard de {b.get('slug')}</h1>
                <table>
                    <tr>
                        <th>Semana</th>
                        <th>Miembros</th>
                        <th>Expulsados</th>
                        <th>Sin vínculo</th>
                        <th>Fallos</th>
                        <th>Fecha</th>
                    </tr>
                    {rows_html}
                </table>
                <div class="footer">
                    Generado automáticamente por <b>TwSubCheck</b> 💜
                </div>
            </body>
            </html>
            """

            return web.Response(text=html, content_type="text/html")

        except Exception as e:
            logging.exception("Error en Dashboard.page:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)
