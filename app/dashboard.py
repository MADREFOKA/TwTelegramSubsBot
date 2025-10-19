# app/dashboard.py
import logging
from aiohttp import web

class Dashboard:
    def __init__(self, db):
        self.db = db

    async def page(self, request: web.Request):
        """Dashboard web básico del streamer (seguro y solo por token)."""
        try:
            slug = request.match_info.get("slug")
            token = request.rel_url.query.get("token")

            if not slug or not token:
                return web.Response(text="❌ Parámetros inválidos.", status=400)

            # Buscar streamer por slug
            b = await self.db.one("SELECT * FROM broadcasters WHERE slug=%s", slug)
            if not b or b.get("dashboard_token") != token:
                return web.Response(text="❌ Acceso no autorizado.", status=403)

            # Intentar leer auditorías por slug
            try:
                logs = await self.db.all("""
                    SELECT etiqueta_semana, total_miembros, expulsados, sin_vinculo, fallos, fecha_auditoria
                    FROM audit_logs
                    WHERE streamer_slug=%s
                    ORDER BY fecha_auditoria DESC
                    LIMIT 20
                """, b["slug"])
            except Exception as e:
                logging.error(f"Error al obtener logs del dashboard: {e}")
                logs = []

            # Construir HTML
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
                <title>Dashboard de {b['slug']}</title>
                <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
                <style>
                    body {{
                        font-family: 'Segoe UI', sans-serif;
                        background-color: #0e0e10;
                        color: #efeff1;
                        margin: 0;
                        padding: 0;
                    }}
                    header {{
                        background-color: #9146FF;
                        padding: 20px;
                        text-align: center;
                        font-size: 26px;
                        font-weight: bold;
                        color: white;
                    }}
                    .container {{
                        padding: 20px;
                        max-width: 1000px;
                        margin: auto;
                    }}
                    .cards {{
                        display: flex;
                        flex-wrap: wrap;
                        justify-content: space-between;
                        margin-top: 20px;
                    }}
                    .card {{
                        flex: 1 1 30%;
                        background-color: #18181b;
                        margin: 10px;
                        padding: 20px;
                        border-radius: 10px;
                        text-align: center;
                        box-shadow: 0 0 10px rgba(145,70,255,0.2);
                    }}
                    .card h2 {{
                        color: #9146FF;
                        font-size: 24px;
                        margin-bottom: 10px;
                    }}
                    .card p {{
                        font-size: 16px;
                        color: #ccc;
                    }}
                    canvas {{
                        margin-top: 40px;
                        background-color: #18181b;
                        padding: 15px;
                        border-radius: 10px;
                        box-shadow: 0 0 10px rgba(145,70,255,0.2);
                    }}
                    table {{
                        width: 100%;
                        border-collapse: collapse;
                        margin-top: 40px;
                        background-color: #18181b;
                        border-radius: 10px;
                        overflow: hidden;
                    }}
                    th, td {{
                        border: 1px solid #2f2f35;
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
                    footer {{
                        margin-top: 50px;
                        text-align: center;
                        font-size: 12px;
                        color: #aaa;
                    }}
                </style>
            </head>
            <body>
                <header>📊 Dashboard de {b['slug']}</header>
            
                <div class="container">
                    <div class="cards">
                        <div class="card">
                            <h2>{len(logs)}</h2>
                            <p>Auditorías Totales</p>
                        </div>
                        <div class="card">
                            <h2>{sum([l.get('expulsados',0) for l in logs])}</h2>
                            <p>Expulsados Totales</p>
                        </div>
                        <div class="card">
                            <h2>{logs[0].get('total_miembros', '—') if logs else '—'}</h2>
                            <p>Miembros Actuales</p>
                        </div>
                    </div>
            
                    <canvas id="chart"></canvas>
            
                    <script>
                        const ctx = document.getElementById('chart');
                        const data = {{
                            labels: { [l.get('etiqueta_semana','-') for l in logs] },
                            datasets: [{{
                                label: 'Expulsados por semana',
                                data: { [l.get('expulsados',0) for l in logs] },
                                backgroundColor: '#9146FF',
                                borderRadius: 6
                            }}]
                        }};
                        new Chart(ctx, {{
                            type: 'bar',
                            data: data,
                            options: {{
                                plugins: {{ legend: {{ display: false }} }},
                                scales: {{
                                    y: {{ beginAtZero: true }},
                                    x: {{ ticks: {{ color: '#ccc' }} }},
                                    y: {{ ticks: {{ color: '#ccc' }} }}
                                }}
                            }}
                        }});
                    </script>
            
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
            
                    <footer>
                        Generado automáticamente por <b>TwSubCheck</b> 💜
                    </footer>
                </div>
            </body>
            </html>
            """
            return web.Response(text=html, content_type="text/html")

        except Exception as e:
            logging.exception("Error en Dashboard.page:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)
