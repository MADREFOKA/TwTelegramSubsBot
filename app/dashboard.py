from aiohttp import web

class Dashboard:
    def __init__(self, db):
        self.db = db

    async def page(self, request):
        slug = request.match_info["slug"]
        token = request.query.get("token")

        b = await self.db.one(
            "SELECT * FROM broadcasters WHERE slug=%s AND dashboard_token=%s", slug, token
        )
        if not b:
            return web.Response(text="❌ Acceso no autorizado.", status=403)

        logs = await self.db.all("""
            SELECT etiqueta_semana, total_miembros, expulsados, sin_vinculo, fallos, fecha_auditoria
            FROM audit_logs
            WHERE streamer_slug=%s
            ORDER BY fecha_auditoria DESC
            LIMIT 20
        """, slug)

        html = f"""
        <html>
        <head>
          <title>Dashboard de {slug}</title>
          <style>
            body {{ background:#0e0e10; color:#efeff1; font-family:sans-serif; margin:40px; }}
            h1 {{ color:#9146FF; }}
            table {{ border-collapse:collapse; width:100%; margin-top:20px; }}
            th,td {{ border:1px solid #2f2f33; padding:8px; text-align:center; }}
            th {{ background-color:#1f1f23; color:#dcdcdc; }}
            tr:nth-child(even) {{ background-color:#18181b; }}
          </style>
        </head>
        <body>
          <h1>📊 Dashboard de {slug}</h1>
          <table>
            <tr><th>Semana</th><th>Miembros</th><th>Expulsados</th><th>Sin vínculo</th><th>Fallos</th><th>Fecha</th></tr>
        """
        for row in logs:
            html += f"<tr><td>{row['etiqueta_semana']}</td><td>{row['total_miembros']}</td><td>{row['expulsados']}</td><td>{row['sin_vinculo']}</td><td>{row['fallos']}</td><td>{row['fecha_auditoria']:%d/%m/%Y}</td></tr>"
        html += "</table></body></html>"

        return web.Response(text=html, content_type="text/html")
