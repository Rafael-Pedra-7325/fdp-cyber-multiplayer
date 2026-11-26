# -*- coding: utf-8 -*-
"""
Servidor Flask + Flask-SocketIO para FDP: Edição Cyber-Noir Multiplayer.
"""

import os
import time
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from cards_data import get_deck_stats
from jogo import FDPRoom, Player, GAME_MODES, AVATARS, generate_room_code

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "fdp-cyber-noir-secret-2026")

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    ping_timeout=60,
    ping_interval=25,
)

# Armazenamento em memória das salas ativas
rooms = {}          # room_code -> FDPRoom
sid_to_session = {} # sid -> {"room_code": str, "token": str}
timer_task_started = False


def broadcast_room_state(room_code):
    room = rooms.get(room_code)
    if not room:
        return
    for token, player in list(room.players.items()):
        if player.connected and player.sid and not player.is_bot:
            state = room.get_state_for_player(token)
            socketio.emit("room_state", state, to=player.sid)


def background_timer_loop():
    """Loop global de 1 segundo para cronômetros e limpeza de salas ociosas."""
    while True:
        socketio.sleep(1)
        now = time.time()
        to_delete = []
        for code, room in list(rooms.items()):
            # Limpa salas vazias ou abandonadas há mais de 2 horas
            if not room.human_tokens() or (now - room.last_activity > 7200):
                to_delete.append(code)
                continue

            if room.timer_active:
                changed = room.tick_timer()
                if changed:
                    broadcast_room_state(code)
                else:
                    socketio.emit(
                        "timer_tick",
                        {"remaining": room.timer_remaining, "active": room.timer_active},
                        room=code,
                    )
        for code in to_delete:
            rooms.pop(code, None)


def ensure_timer_started():
    global timer_task_started
    if not timer_task_started:
        timer_task_started = True
        socketio.start_background_task(background_timer_loop)


# ==================== ROTAS HTTP ====================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/meta")
def api_meta():
    return jsonify({
        "deck_stats": get_deck_stats(),
        "game_modes": GAME_MODES,
        "avatars": AVATARS,
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok", "active_rooms": len(rooms)})


# ==================== EVENTOS SOCKET.IO ====================

@socketio.on("connect")
def handle_connect():
    ensure_timer_started()


@socketio.on("create_room")
def handle_create_room(data):
    ensure_timer_started()
    name = (data.get("name") or "Anfitrião").strip()
    avatar = data.get("avatar") or "👑"
    token = data.get("token")

    code = generate_room_code(rooms.keys())
    host = Player(name=name, sid=request.sid, avatar=avatar, token=token)
    room = FDPRoom(code=code, host_player=host)
    rooms[code] = room

    join_room(code)
    sid_to_session[request.sid] = {"room_code": code, "token": host.token}

    emit("joined_success", {"room_code": code, "token": host.token})
    broadcast_room_state(code)


@socketio.on("join_room_request")
def handle_join_room(data):
    ensure_timer_started()
    code = (data.get("room_code") or "").strip().upper()
    name = (data.get("name") or "Jogador").strip()
    avatar = data.get("avatar")
    token = data.get("token")

    room = rooms.get(code)
    if not room:
        emit("error_msg", {"message": f"Sala '{code}' não encontrada. Verifique o código!"})
        return

    player, reconnected, err = room.add_player(name=name, sid=request.sid, avatar=avatar, token=token)
    if err:
        emit("error_msg", {"message": err})
        return

    join_room(code)
    sid_to_session[request.sid] = {"room_code": code, "token": player.token}

    emit("joined_success", {"room_code": code, "token": player.token, "reconnected": reconnected})
    if not reconnected:
        socketio.emit("toast", {"message": f"{player.avatar} {player.name} entrou na sala!"}, room=code)
    broadcast_room_state(code)


@socketio.on("leave_room_request")
def handle_leave_room():
    sess = sid_to_session.pop(request.sid, None)
    if not sess:
        return
    code = sess["room_code"]
    token = sess["token"]
    room = rooms.get(code)
    leave_room(code)
    if room:
        p = room.players.get(token)
        pname = f"{p.avatar} {p.name}" if p else "Um jogador"
        room.remove_player(token)
        if not room.human_tokens():
            rooms.pop(code, None)
        else:
            socketio.emit("toast", {"message": f"{pname} saiu da sala."}, room=code)
            broadcast_room_state(code)


@socketio.on("disconnect")
def handle_disconnect():
    sess = sid_to_session.pop(request.sid, None)
    if not sess:
        return
    code = sess["room_code"]
    room = rooms.get(code)
    if room:
        room.disconnect_player(request.sid)
        broadcast_room_state(code)


@socketio.on("update_config")
def handle_update_config(data):
    sess = sid_to_session.get(request.sid)
    if not sess:
        return
    room = rooms.get(sess["room_code"])
    if not room:
        return
    ok, err = room.update_config(sess["token"], data)
    if not ok:
        emit("error_msg", {"message": err})
        return
    broadcast_room_state(sess["room_code"])


@socketio.on("kick_player")
def handle_kick_player(data):
    sess = sid_to_session.get(request.sid)
    if not sess:
        return
    room = rooms.get(sess["room_code"])
    if not room or sess["token"] != room.host_token:
        return
    target_id = data.get("player_id")
    if target_id and target_id != room.host_token and target_id in room.players:
        target = room.players[target_id]
        if target.sid:
            socketio.emit("kicked", {"message": "Você foi removido da sala pelo Anfitrião."}, to=target.sid)
        room.remove_player(target_id)
        broadcast_room_state(sess["room_code"])


@socketio.on("start_game")
def handle_start_game():
    sess = sid_to_session.get(request.sid)
    if not sess:
        return
    room = rooms.get(sess["room_code"])
    if not room:
        return
    ok, err = room.start_game(sess["token"])
    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("sound_cue", {"sound": "start"}, room=sess["room_code"])
    broadcast_room_state(sess["room_code"])


@socketio.on("submit_cards")
def handle_submit_cards(data):
    sess = sid_to_session.get(request.sid)
    if not sess:
        return
    room = rooms.get(sess["room_code"])
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
    socketio.emit("sound_cue", {"sound": "card"}, room=sess["room_code"])
    broadcast_room_state(sess["room_code"])


@socketio.on("reboot_hand")
def handle_reboot_hand(data):
    sess = sid_to_session.get(request.sid)
    if not sess:
        return
    room = rooms.get(sess["room_code"])
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
    broadcast_room_state(sess["room_code"])


@socketio.on("reveal_submission")
def handle_reveal_submission(data):
    sess = sid_to_session.get(request.sid)
    if not sess:
        return
    room = rooms.get(sess["room_code"])
    if not room:
        return
    ok, err = room.reveal_submission(sess["token"], data.get("entry_id"))
    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("sound_cue", {"sound": "reveal"}, room=sess["room_code"])
    broadcast_room_state(sess["room_code"])


@socketio.on("reveal_all")
def handle_reveal_all():
    sess = sid_to_session.get(request.sid)
    if not sess:
        return
    room = rooms.get(sess["room_code"])
    if not room:
        return
    ok, err = room.reveal_all_submissions(sess["token"])
    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("sound_cue", {"sound": "reveal"}, room=sess["room_code"])
    broadcast_room_state(sess["room_code"])


@socketio.on("pick_winner")
def handle_pick_winner(data):
    sess = sid_to_session.get(request.sid)
    if not sess:
        return
    room = rooms.get(sess["room_code"])
    if not room:
        return
    ok, err = room.judge_pick_winner(sess["token"], data.get("entry_id"))
    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("sound_cue", {"sound": "win"}, room=sess["room_code"])
    broadcast_room_state(sess["room_code"])


@socketio.on("cast_vote")
def handle_cast_vote(data):
    sess = sid_to_session.get(request.sid)
    if not sess:
        return
    room = rooms.get(sess["room_code"])
    if not room:
        return
    ok, err = room.cast_vote(sess["token"], data.get("entry_id"))
    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("sound_cue", {"sound": "card"}, room=sess["room_code"])
    broadcast_room_state(sess["room_code"])


@socketio.on("next_round")
def handle_next_round():
    sess = sid_to_session.get(request.sid)
    if not sess:
        return
    room = rooms.get(sess["room_code"])
    if not room:
        return
    ok, err = room.next_round_trigger(sess["token"])
    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("sound_cue", {"sound": "start"}, room=sess["room_code"])
    broadcast_room_state(sess["room_code"])


@socketio.on("force_advance")
def handle_force_advance():
    sess = sid_to_session.get(request.sid)
    if not sess:
        return
    room = rooms.get(sess["room_code"])
    if not room:
        return
    ok, err = room.force_advance(sess["token"])
    if not ok:
        emit("error_msg", {"message": err})
        return
    socketio.emit("toast", {"message": "⚡ O Anfitrião avançou a fase da rodada!"}, room=sess["room_code"])
    broadcast_room_state(sess["room_code"])


@socketio.on("return_lobby")
def handle_return_lobby():
    sess = sid_to_session.get(request.sid)
    if not sess:
        return
    room = rooms.get(sess["room_code"])
    if not room:
        return
    ok, err = room.reset_to_lobby(sess["token"])
    if not ok:
        emit("error_msg", {"message": err})
        return
    broadcast_room_state(sess["room_code"])


@socketio.on("send_reaction")
def handle_reaction(data):
    sess = sid_to_session.get(request.sid)
    if not sess:
        return
    room = rooms.get(sess["room_code"])
    if not room:
        return
    emoji = str(data.get("emoji", "💀"))[:4]
    p = room.players.get(sess["token"])
    socketio.emit(
        "floating_reaction",
        {"emoji": emoji, "sender": p.name if p else ""},
        room=sess["room_code"],
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=True)
