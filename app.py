# -*- coding: utf-8 -*-
"""
Servidor Flask + Flask-SocketIO para FDP: Edição Cyber-Noir Multiplayer.
"""

import os
import secrets
import threading
import time

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.middleware.proxy_fix import ProxyFix

from cards_data import get_deck_stats
from jogo import AVATARS, FDPRoom, GAME_MODES, Player, generate_room_code

ASYNC_MODE = "threading"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# Em produção defina ALLOWED_ORIGINS (ex.: https://seu-app.onrender.com).
# Várias origens: separe por vírgula. Sem a variável, aceita qualquer origem.
_origins = os.environ.get("ALLOWED_ORIGINS", "*").strip()
ALLOWED_ORIGINS = "*" if _origins == "*" else [o.strip() for o in _origins.split(",") if o.strip()]

socketio = SocketIO(
    app,
    async_mode=ASYNC_MODE,
    cors_allowed_origins=ALLOWED_ORIGINS,
    ping_timeout=60,
    ping_interval=25,
)

# Armazenamento em memória das salas ativas (requer -w 1 no Gunicorn)
rooms = {}  # room_code -> FDPRoom
sid_to_session = {}  # sid -> {"room_code": str, "token": str}
state_lock = threading.RLock()
timer_task_started = False


def broadcast_room_state(room_code):
    with state_lock:
        room = rooms.get(room_code)
        if not room:
            return
        payloads = []
        for token, player in list(room.players.items()):
            if player.connected and player.sid and not player.is_bot:
                payloads.append((player.sid, room.get_state_for_player(token)))

    for sid, state in payloads:
        socketio.emit("room_state", state, to=sid)


def cleanup_old_sids_for_token(room_code, token, current_sid):
    """Remove SIDs antigos órfãos quando um jogador reconecta."""
    with state_lock:
        stale_sids = [
            sid
            for sid, sess in list(sid_to_session.items())
            if sid != current_sid
            and sess["room_code"] == room_code
            and sess["token"] == token
        ]
        for old_sid in stale_sids:
            sid_to_session.pop(old_sid, None)

    for old_sid in stale_sids:
        try:
            leave_room(room_code, sid=old_sid)
        except Exception:
            pass


def detach_current_sid_from_other_room(current_sid, target_room_code=None):
    """Garante que o mesmo socket não fique preso em duas salas simultaneamente."""
    old_code = None
    with state_lock:
        prev = sid_to_session.get(current_sid)
        if prev and prev["room_code"] != target_room_code:
            old_code = prev["room_code"]
            sid_to_session.pop(current_sid, None)
            old_room = rooms.get(old_code)
            if old_room:
                old_room.disconnect_player(current_sid)

    if old_code:
        try:
            leave_room(old_code, sid=current_sid)
        except Exception:
            pass
        broadcast_room_state(old_code)


def cleanup_sessions_for_deleted_room(room_code):
    """Limpa entradas órfãs de sid_to_session quando uma sala expira."""
    with state_lock:
        stale = [
            sid
            for sid, sess in list(sid_to_session.items())
            if sess["room_code"] == room_code
        ]
        for sid in stale:
            sid_to_session.pop(sid, None)


def background_timer_loop():
    """Loop global de 1 segundo para cronômetros e limpeza de salas ociosas."""
    while True:
        socketio.sleep(1)
        try:
            now = time.time()
            to_delete = []
            rooms_to_broadcast = []
            ticks_to_emit = []

            with state_lock:
                for code, room in list(rooms.items()):
                    # Limpa salas vazias ou abandonadas há mais de 2 horas
                    if not room.human_tokens() or (now - room.last_activity > 7200):
                        to_delete.append(code)
                        continue
                    if room.maintenance(now):
                        rooms_to_broadcast.append(code)
                    if room.timer_active:
                        changed = room.tick_timer()
                        if changed:
                            rooms_to_broadcast.append(code)
                        else:
                            ticks_to_emit.append(
                                (
                                    code,
                                    {
                                        "remaining": room.timer_remaining,
                                        "active": room.timer_active,
                                    },
                                )
                            )

                for code in to_delete:
                    rooms.pop(code, None)
                    cleanup_sessions_for_deleted_room(code)

            for code in rooms_to_broadcast:
                broadcast_room_state(code)

            for code, payload in ticks_to_emit:
                socketio.emit("timer_tick", payload, to=code)

        except Exception:
            app.logger.exception("Erro no background_timer_loop")


def ensure_timer_started():
    global timer_task_started
    with state_lock:
        if not timer_task_started:
            timer_task_started = True
            socketio.start_background_task(background_timer_loop)


# ==================== ROTAS HTTP ====================


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/meta")
def api_meta():
    return jsonify(
        {
            "deck_stats": get_deck_stats(),
            "game_modes": GAME_MODES,
            "avatars": AVATARS,
        }
    )


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/health")
def health():
    with state_lock:
        active_rooms = len(rooms)
    return jsonify(
        {
            "status": "ok",
            "active_rooms": active_rooms,
            "async_mode": ASYNC_MODE,
        }
    )


# ==================== EVENTOS SOCKET.IO ====================


@socketio.on("connect")
def handle_connect():
    ensure_timer_started()


@socketio.on("create_room")
def handle_create_room(data):
    ensure_timer_started()
    data = data or {}
    name = (data.get("name") or "Anfitrião").strip()
    avatar = data.get("avatar") or "😈"
    token = data.get("token")

    detach_current_sid_from_other_room(request.sid)

    with state_lock:
        code = generate_room_code(rooms.keys())
        host = Player(name=name, sid=request.sid, avatar=avatar, token=token)
        room = FDPRoom(code=code, host_player=host)
        rooms[code] = room
        sid_to_session[request.sid] = {"room_code": code, "token": host.token}

    join_room(code)
    emit("joined_success", {"room_code": code, "token": host.token})
    broadcast_room_state(code)


@socketio.on("join_room_request")
def handle_join_room(data):
    ensure_timer_started()
    data = data or {}
    code = (data.get("room_code") or "").strip().upper()
    name = (data.get("name") or "Jogador").strip()
    avatar = data.get("avatar")
    token = data.get("token")
    auto_reconnect = bool(data.get("auto_reconnect", False))

    detach_current_sid_from_other_room(request.sid, target_room_code=code)

    with state_lock:
        room = rooms.get(code)
        if not room:
            if auto_reconnect:
                emit("session_expired", {"room_code": code})
            else:
                emit(
                    "error_msg",
                    {"message": f"Sala '{code}' não encontrada. Verifique o código!"},
                )
            return

        player, reconnected, err = room.add_player(
            name=name, sid=request.sid, avatar=avatar, token=token
        )
        if err:
            if auto_reconnect:
                emit("session_expired", {"room_code": code})
            else:
                emit("error_msg", {"message": err})
            return

        player_token = player.token
        player_avatar = player.avatar
        player_name = player.name
        sid_to_session[request.sid] = {"room_code": code, "token": player_token}

    cleanup_old_sids_for_token(code, player_token, request.sid)
    join_room(code)
    emit(
        "joined_success",
        {"room_code": code, "token": player_token, "reconnected": reconnected},
    )
    if not reconnected:
        socketio.emit(
            "toast",
            {"message": f"{player_avatar} {player_name} entrou na sala!"},
            to=code,
        )
    broadcast_room_state(code)


@socketio.on("leave_room_request")
def handle_leave_room():
    with state_lock:
        sess = sid_to_session.pop(request.sid, None)
        if not sess:
            return
        code = sess["room_code"]
        token = sess["token"]
        room = rooms.get(code)
        pname = "Um jogador"
        room_deleted = False
        if room:
            p = room.players.get(token)
            if p:
                pname = f"{p.avatar} {p.name}"
            room.remove_player(token)
            if not room.human_tokens():
                rooms.pop(code, None)
                cleanup_sessions_for_deleted_room(code)
                room_deleted = True

    leave_room(code)
    if not room_deleted:
        socketio.emit("toast", {"message": f"{pname} saiu da sala."}, to=code)
        broadcast_room_state(code)


@socketio.on("disconnect")
def handle_disconnect():
    code = None
    with state_lock:
        sess = sid_to_session.pop(request.sid, None)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if room:
            room.disconnect_player(request.sid)

    if code:
        broadcast_room_state(code)


@socketio.on("update_config")
def handle_update_config(data):
    with state_lock:
        sess = sid_to_session.get(request.sid)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if not room:
            return
        ok, err = room.update_config(sess["token"], data or {})

    if not ok:
        emit("error_msg", {"message": err})
        return
    broadcast_room_state(code)


@socketio.on("kick_player")
def handle_kick_player(data):
    target_sid = None
    code = None
    kicked = False

    with state_lock:
        sess = sid_to_session.get(request.sid)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if not room or sess["token"] != room.host_token:
            return
        target_id = room.token_from_pid((data or {}).get("player_id"))
        if target_id and target_id != room.host_token and target_id in room.players:
            target = room.players[target_id]
            target_sid = target.sid
            if target_sid:
                sid_to_session.pop(target_sid, None)
            room.remove_player(target_id)
            kicked = True

    if kicked:
        if target_sid:
            socketio.emit(
                "kicked",
                {"message": "Você foi removido da sala pelo Anfitrião."},
                to=target_sid,
            )
            try:
                leave_room(code, sid=target_sid)
            except Exception:
                pass
        broadcast_room_state(code)


@socketio.on("start_game")
def handle_start_game():
    with state_lock:
        sess = sid_to_session.get(request.sid)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if not room:
            return
        ok, err = room.start_game(sess["token"])

    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("sound_cue", {"sound": "start"}, to=code)
    broadcast_room_state(code)


@socketio.on("submit_cards")
def handle_submit_cards(data):
    data = data or {}
    with state_lock:
        sess = sid_to_session.get(request.sid)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if not room:
            return
        ok, err = room.submit_cards(
            player_token=sess["token"],
            card_indices=data.get("indices", []),
            custom_texts=data.get("custom_texts", {}),
            is_bet=bool(data.get("is_bet", False)),
        )

    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("sound_cue", {"sound": "card"}, to=code)
    broadcast_room_state(code)


@socketio.on("reboot_hand")
def handle_reboot_hand(data):
    data = data or {}
    with state_lock:
        sess = sid_to_session.get(request.sid)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if not room:
            return
        ok, err = room.reboot_hand(
            player_token=sess["token"],
            card_indices=data.get("indices", []),
            use_free=bool(data.get("use_free", False)),
        )

    if not ok:
        emit("error_msg", {"message": err})
        return
    emit("toast", {"message": "Cartas trocadas com sucesso!"})
    broadcast_room_state(code)


@socketio.on("reveal_submission")
def handle_reveal_submission(data):
    with state_lock:
        sess = sid_to_session.get(request.sid)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if not room:
            return
        ok, err = room.reveal_submission(sess["token"], (data or {}).get("entry_id"))

    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("sound_cue", {"sound": "reveal"}, to=code)
    broadcast_room_state(code)


@socketio.on("reveal_all")
def handle_reveal_all():
    with state_lock:
        sess = sid_to_session.get(request.sid)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if not room:
            return
        ok, err = room.reveal_all_submissions(sess["token"])

    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("sound_cue", {"sound": "reveal"}, to=code)
    broadcast_room_state(code)


@socketio.on("pick_winner")
def handle_pick_winner(data):
    with state_lock:
        sess = sid_to_session.get(request.sid)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if not room:
            return
        ok, err = room.judge_pick_winner(sess["token"], (data or {}).get("entry_id"))

    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("sound_cue", {"sound": "win"}, to=code)
    broadcast_room_state(code)


@socketio.on("cast_vote")
def handle_cast_vote(data):
    with state_lock:
        sess = sid_to_session.get(request.sid)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if not room:
            return
        ok, err = room.cast_vote(sess["token"], (data or {}).get("entry_id"))

    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("sound_cue", {"sound": "card"}, to=code)
    broadcast_room_state(code)


@socketio.on("next_round")
def handle_next_round():
    with state_lock:
        sess = sid_to_session.get(request.sid)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if not room:
            return
        ok, err = room.next_round_trigger(sess["token"])

    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("sound_cue", {"sound": "start"}, to=code)
    broadcast_room_state(code)


@socketio.on("force_advance")
def handle_force_advance():
    with state_lock:
        sess = sid_to_session.get(request.sid)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if not room:
            return
        ok, err = room.force_advance(sess["token"])

    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit(
        "toast",
        {"message": "⚡ O Anfitrião avançou a fase da rodada!"},
        to=code,
    )
    broadcast_room_state(code)


@socketio.on("return_lobby")
def handle_return_lobby():
    with state_lock:
        sess = sid_to_session.get(request.sid)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if not room:
            return
        ok, err = room.reset_to_lobby(sess["token"])

    if not ok:
        emit("error_msg", {"message": err})
        return
    broadcast_room_state(code)


@socketio.on("send_reaction")
def handle_reaction(data):
    with state_lock:
        sess = sid_to_session.get(request.sid)
        if not sess:
            return
        code = sess["room_code"]
        room = rooms.get(code)
        if not room:
            return
        emoji = str((data or {}).get("emoji", "😂"))[:4]
        p = room.players.get(sess["token"])
        sender = p.name if p else ""

    socketio.emit(
        "floating_reaction",
        {"emoji": emoji, "sender": sender},
        to=code,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=debug,
        allow_unsafe_werkzeug=True,
    )
