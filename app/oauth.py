# app/oauth.py
import time
import logging
import requests

from aiohttp import web

from app.auditor import fetch_all_subs, save_user_token
from app.utils.messages import (
    setup_success,
    viewer_is_owner,
    viewer_is_sub,
    viewer_not_sub,
)


class OAuthHandlers:
    def __init__(self, cfg, db, send_tg, refresh_twitch_token, days_until, cipher):
        self.cfg = cfg
        self.db = db
        self.send_tg = send_tg
        self.refresh_twitch_token = refresh_twitch_token
        self.days_until = days_until
        self.cipher = cipher

    # ───────────────────────────────────────────────
    # CALLBACK SETUP (STREAMER)
    # ───────────────────────────────────────────────
    async def twitch_callback_setup(self, request: web.Request):
        try:
            q = request.rel_url.query
            code, state = q.get("code"), q.get("state")
            if not code or not state:
                return web.Response(text="❌ Falta code/state.", status=400)

            row = await self.db.one(
                "SELECT * FROM oauth_states WHERE state=%s AND purpose='broadcaster_setup'",
                state
            )
            if not row:
                return web.Response(text="❌ Estado inválido o caducado.", status=400)

            # Intercambio del código por tokens
            data = requests.post(self.cfg.TW_TOKEN, data={
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": f"{self.cfg.base_url}/twitch/setup/callback"
            }, timeout=15).json()

            if "access_token" not in data:
                logging.error(f"❌ Error al obtener token: {data}")
                return web.Response(text="❌ No se pudo obtener el token de Twitch.", status=400)

            access_token = data["access_token"]
            refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 0)

            # Obtener info del canal
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Client-Id": self.cfg.client_id,
            }
            me = requests.get(f"{self.cfg.TW_API}/users", headers=headers, timeout=15).json()["data"][0]
            slug = me["login"].lower()
            avatar = me.get("profile_image_url")

            # Cifrar tokens
            enc_access = self.cipher.encrypt(access_token)
            enc_refresh = self.cipher.encrypt(refresh_token)

            # Crear nuevo token para dashboard
            import secrets
            dashboard_token = secrets.token_urlsafe(32)

            # Guardar broadcaster
            await self.db.exec(
                """
                INSERT INTO broadcasters (
                    broadcaster_id, display_name, slug, access_token, refresh_token,
                    token_obtained_at, token_expires_in, owner_telegram_id, dashboard_token, profile_image_url
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (broadcaster_id) DO UPDATE SET
                    display_name=EXCLUDED.display_name,
                    slug=EXCLUDED.slug,
                    access_token=EXCLUDED.access_token,
                    refresh_token=EXCLUDED.refresh_token,
                    token_obtained_at=EXCLUDED.token_obtained_at,
                    token_expires_in=EXCLUDED.token_expires_in,
                    owner_telegram_id=EXCLUDED.owner_telegram_id,
                    dashboard_token=EXCLUDED.dashboard_token,
                    profile_image_url=EXCLUDED.profile_image_url
                """,
                me["id"], me["display_name"], slug,
                enc_access, enc_refresh, int(time.time()), expires_in,
                row["telegram_id"], dashboard_token, avatar
            )

            dash_link = f"{self.cfg.base_url}/dashboard/{slug}?token={dashboard_token}"

            # Mensaje principal con botones inline (Dashboard + Guía)
            msg, keyboard = setup_success(me["display_name"], self.cfg.bot_username, slug, dash_link)

            # Enviar mensaje sin previsualización del enlace
            await self.send_tg(
                row["telegram_id"],
                msg,
                html=True,
                disable_preview=True,
                reply_markup=keyboard
            )

            return web.Response(
                text=(
                    f"<meta http-equiv='refresh' content='0; url=https://t.me/{self.cfg.bot_username}'>"
                    "<h3>✅ Vinculación completada. Puedes volver a Telegram.</h3>"
                ),
                content_type="text/html"
            )

        except Exception as e:
            logging.exception("Error en twitch_callback_setup:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)

    # ───────────────────────────────────────────────
    # 🔥 MEJORADO: Invitación premium con retry
    # ───────────────────────────────────────────────
    def _create_one_time_premium_invite(self, group_id: int, ttl_seconds: int = 600, retries: int = 3):
        """
        Crea link de invitación de 1 solo uso con reintentos.
        Devuelve (link, is_single_use)
        """
        if not group_id:
            return None, False

        for attempt in range(retries):
            try:
                url = f"https://api.telegram.org/bot{self.cfg.token}/createChatInviteLink"
                payload = {
                    "chat_id": group_id,
                    "member_limit": 1,  # 1 solo uso
                    "expire_date": int(time.time()) + ttl_seconds,
                    "name": f"premium-1use-{int(time.time())}",
                }
                r = requests.post(url, json=payload, timeout=10)
                
                if r.status_code == 200:
                    link = r.json().get("result", {}).get("invite_link")
                    if link:
                        logging.info(f"✅ Link 1-uso creado: {link}")
                        return link, True
                else:
                    logging.warning(f"⚠️ Intento {attempt+1}/{retries} falló: {r.status_code} - {r.text}")
                    
            except Exception as e:
                logging.warning(f"⚠️ Error creando link 1-uso (intento {attempt+1}/{retries}): {e}")
            
            if attempt < retries - 1:
                time.sleep(0.5)  # Pequeña pausa antes de reintentar

        logging.error("❌ No se pudo crear link de 1 uso después de múltiples intentos")
        return None, False

    # ───────────────────────────────────────────────
    # 🔥 MEJORADO: Callback de usuario con verificación robusta
    # ───────────────────────────────────────────────
    async def twitch_callback_user(self, request: web.Request):
        try:
            q = request.rel_url.query
            code, state = q.get("code"), q.get("state")
            if not code or not state:
                return web.Response(text="❌ Falta parámetros.", status=400)

            # Buscar estado OAuth válido
            row = await self.db.one(
                "SELECT * FROM oauth_states WHERE state=%s AND purpose='user_link'",
                state
            )
            if not row:
                return web.Response(text="❌ Estado inválido o caducado.", status=400)

            # Intercambio del código por token
            data = requests.post(self.cfg.TW_TOKEN, data={
                "client_id": self.cfg.client_id,
                "client_secret": self.cfg.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{self.cfg.base_url}/twitch/callback"
            }, timeout=15).json()

            if "access_token" not in data:
                return web.Response(text=f"Error al obtener token: {data}", status=400)

            access_token = data["access_token"]
            user_refresh_token = data.get("refresh_token")
            user_expires_in = data.get("expires_in", 0)
            headers_user = {
                "Authorization": f"Bearer {access_token}",
                "Client-Id": self.cfg.client_id
            }

            # Datos del usuario de Twitch
            me = requests.get(
                f"{self.cfg.TW_API}/users",
                headers=headers_user,
                timeout=15
            ).json()["data"][0]

            # Broadcaster asociado
            b = await self.db.one(
                "SELECT * FROM broadcasters WHERE broadcaster_id=%s",
                row["broadcaster_id"]
            )
            if not b:
                return web.Response(text="Streamer no encontrado.", status=400)

            # 🔥 MEJORADO: Verificación de suscripción más robusta
            is_sub = await self._verify_subscription_robust(
                user_id=me["id"],
                broadcaster_id=b["broadcaster_id"],
                headers_user=headers_user,
                broadcaster_row=b
            )

            logging.info(f"🔍 Usuario {me['login']} → Sub={is_sub} de {b['display_name']}")

            # Guardar el token del usuario: la auditoría lo usa para confirmar su sub
            # antes de expulsar (Twitch a veces dice "no sub" desde el lado del canal)
            try:
                await save_user_token(self.db, me["id"], row["telegram_id"],
                                      access_token, user_refresh_token, user_expires_in)
            except Exception as e:
                logging.error(f"❌ No se pudo guardar el token de usuario: {e}")

            # Si se re-verifica como sub, se cancela cualquier aviso de expulsión pendiente
            if is_sub and b.get("group_id"):
                try:
                    await self.db.exec(
                        "DELETE FROM audit_strikes WHERE group_id=%s AND telegram_id=%s",
                        b["group_id"], row["telegram_id"])
                except Exception as e:
                    logging.warning(f"⚠️ No se pudo limpiar audit_strikes: {e}")

            # 🔥 MEJORADO: Actualizar telegram_users CON validación
            await self._update_telegram_user(
                telegram_id=row["telegram_id"],
                twitch_id=me["id"],
                twitch_login=me["login"],
                is_sub=is_sub,
                broadcaster_id=b["broadcaster_id"]
            )

            # 🔥 NUEVO: Si es sub Y hay grupo premium → guardar en group_members INMEDIATAMENTE
            if is_sub and b.get("group_id"):
                await self._add_to_group_members_if_sub(
                    telegram_id=row["telegram_id"],
                    twitch_id=me["id"],
                    twitch_login=me["login"],
                    group_id=b["group_id"],
                    broadcaster_id=b["broadcaster_id"],
                    slug=b["slug"]
                )

            # Generar links de invitación
            premium_link = None
            is_single_use = False
            
            if b.get("group_id") and is_sub:
                premium_link, is_single_use = self._create_one_time_premium_invite(
                    b["group_id"], 
                    ttl_seconds=600
                )
                
                # 🔥 MEJORADO: Si no se pudo crear link de 1 uso, NO usar el persistente
                if not is_single_use:
                    logging.warning(f"⚠️ No se pudo crear link 1-uso para {me['login']}, NO se enviará link")
                    premium_link = None

            # Mensajes para el usuario
            if me["id"] == b["broadcaster_id"]:
                msg, keyboard = viewer_is_owner(me["display_name"], b["display_name"], premium_link)
            elif is_sub:
                msg, keyboard = viewer_is_sub(me["display_name"], b["display_name"], premium_link)
            else:
                msg, keyboard = viewer_not_sub(me["display_name"], b["display_name"], b.get("free_invite_link"))

            # Enviar mensaje
            await self.send_tg(
                row["telegram_id"],
                msg,
                html=True,
                reply_markup=keyboard
            )

            return web.Response(
                text=(
                    f"<meta http-equiv='refresh' content='0; url=https://t.me/{self.cfg.bot_username}'>"
                    "<h3>✅ Verificación completa. Puedes volver a Telegram.</h3>"
                ),
                content_type="text/html"
            )

        except Exception as e:
            logging.exception("Error en twitch_callback_user:")
            return web.Response(text=f"❌ Error interno: {e}", status=500)

    # ───────────────────────────────────────────────
    # 🔥 NUEVA FUNCIÓN: Verificación robusta de suscripción
    # ───────────────────────────────────────────────
    async def _verify_subscription_robust(
        self, 
        user_id: str, 
        broadcaster_id: str, 
        headers_user: dict,
        broadcaster_row: dict
    ) -> bool:
        """
        Verifica suscripción con 3 métodos en cascada:
        1. API de usuario (/subscriptions/user)
        2. Lista completa del broadcaster (/subscriptions)
        3. Reintento tras refresh de token broadcaster
        """
        # Método 1: API del usuario
        try:
            r = requests.get(
                f"{self.cfg.TW_API}/subscriptions/user",
                headers=headers_user,
                params={"broadcaster_id": broadcaster_id, "user_id": user_id},
                timeout=15
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    logging.info(f"✅ Sub confirmada vía /subscriptions/user")
                    return True
                logging.info(f"ℹ️ /subscriptions/user devolvió lista vacía")
            else:
                logging.warning(f"⚠️ /subscriptions/user falló: {r.status_code}")
        except Exception as e:
            logging.warning(f"⚠️ Excepción en /subscriptions/user: {e}")

        # Método 2: Lista completa del broadcaster
        try:
            b_access = await self.refresh_twitch_token(broadcaster_row)
            headers_b = {
                "Authorization": f"Bearer {b_access}",
                "Client-Id": self.cfg.client_id,
            }
            
            subs_set, err = fetch_all_subs(broadcaster_id, headers_b)
            
            # Método 3: Si falló con 401, reintenta tras refresh
            if err and err[0] == 401:
                logging.warning("⚠️ Token broadcaster caducado, refrescando...")
                b_access = await self.refresh_twitch_token(broadcaster_row)
                headers_b["Authorization"] = f"Bearer {b_access}"
                subs_set, err = fetch_all_subs(broadcaster_id, headers_b)

            if not err and subs_set:
                is_in_list = str(user_id) in subs_set
                logging.info(f"✅ Lista completa obtenida: usuario {'SÍ' if is_in_list else 'NO'} está")
                return is_in_list
            else:
                logging.error(f"❌ No se pudo obtener lista de subs: {err}")
                
        except Exception as e:
            logging.exception(f"❌ Error obteniendo lista completa de subs: {e}")

        # Si todos los métodos fallaron, asumir NO sub por seguridad
        logging.warning(f"⚠️ No se pudo verificar suscripción de {user_id}, asumiendo NO sub")
        return False

    # ───────────────────────────────────────────────
    # 🔥 NUEVA FUNCIÓN: Actualizar telegram_users
    # ───────────────────────────────────────────────
    async def _update_telegram_user(
        self,
        telegram_id: int,
        twitch_id: str,
        twitch_login: str,
        is_sub: bool,
        broadcaster_id: str
    ):
        """Actualiza o crea registro en telegram_users para un streamer específico"""
        try:
            await self.db.exec("""
                INSERT INTO telegram_users (
                    telegram_id, streamer_id, linked_twitch_id, 
                    twitch_login, is_subscribed
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (telegram_id, streamer_id)
                DO UPDATE SET
                    linked_twitch_id = EXCLUDED.linked_twitch_id,
                    twitch_login = EXCLUDED.twitch_login,
                    is_subscribed = EXCLUDED.is_subscribed
            """, telegram_id, broadcaster_id, twitch_id, twitch_login, is_sub)
            
            logging.info(f"💾 telegram_users actualizado: TG={telegram_id} TW={twitch_login} Sub={is_sub}")
        except Exception as e:
            logging.error(f"❌ Error actualizando telegram_users: {e}")

    # ───────────────────────────────────────────────
    # 🔥 NUEVA FUNCIÓN: Agregar a group_members si es sub
    # ───────────────────────────────────────────────
    async def _add_to_group_members_if_sub(
        self,
        telegram_id: int,
        twitch_id: str,
        twitch_login: str,
        group_id: int,
        broadcaster_id: str,
        slug: str
    ):
        """
        Agrega el usuario a group_members si verificó que es sub.
        Esto asegura que esté registrado ANTES de entrar al grupo.
        """
        try:
            await self.db.exec("""
                INSERT INTO group_members (
                    group_id, telegram_id, twitch_id, twitch_login,
                    broadcaster_id, streamer_slug, is_subscribed, joined_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, TRUE, NOW())
                ON CONFLICT (group_id, telegram_id)
                DO UPDATE SET
                    twitch_id = EXCLUDED.twitch_id,
                    twitch_login = EXCLUDED.twitch_login,
                    is_subscribed = TRUE
            """, group_id, telegram_id, twitch_id, twitch_login, broadcaster_id, slug)
            
            logging.info(f"✅ Pre-registrado en group_members: {twitch_login} → {slug}")
        except Exception as e:
            logging.error(f"❌ Error pre-registrando en group_members: {e}")
