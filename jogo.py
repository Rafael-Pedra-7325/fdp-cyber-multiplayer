# -*- coding: utf-8 -*-
"""
Motor completo de regras multiplayer do FDP (Foi De Propósito).
Suporta até 12 jogadores por sala, 8 modos de jogo, reconexão por token,
cartas coringa editáveis e controle de tempo automático.
"""

import random
import string
import time
import uuid
from cards_data import WHITE_DECKS, BLACK_DECKS, DECK_METADATA

WILDCARD_TOKEN = "__WILDCARD__"

GAME_MODES = {
    "classico": {
        "id": "classico",
        "name": "Clássico (Original)",
        "desc": "O Juiz (FDP da Rodada) rotaciona e escolhe a combinação mais absurda.",
        "icon": "👑",
    },
    "rando": {
        "id": "rando",
        "name": "Rando Cardrissian",
        "desc": "Um bot fantasma joga cartas aleatórias toda rodada. Se ele vencer o jogo, todos os humanos passam vergonha!",
        "icon": "🤖",
    },
    "aposta": {
        "id": "aposta",
        "name": "Aposta Arriscada (Dobro ou Nada)",
        "desc": "Gaste 1 ponto acumulado para jogar uma 2ª resposta na rodada. O vencedor leva o pote inteiro!",
        "icon": "🎰",
    },
    "reinado": {
        "id": "reinado",
        "name": "Reinado do FDP",
        "desc": "Quem vencer a rodada assume o trono e se torna o próximo Juiz.",
        "icon": "⚔️",
    },
    "democracia": {
        "id": "democracia",
        "name": "Democracia Caótica",
        "desc": "Sem Juiz! Todos jogam e todos votam na melhor resposta (não vale votar na própria).",
        "icon": "🗳️",
    },
    "relampago": {
        "id": "relampago",
        "name": "Modo Relâmpago (Speed FDP)",
        "desc": "Cronômetro implacável de 25 segundos. Quem dormir no ponto joga carta aleatória!",
        "icon": "⚡",
    },
    "caos_total": {
        "id": "caos_total",
        "name": "Caos Supremo (+Coringas & Apostas)",
        "desc": "Ativa Rando Cardrissian, Apostas, Cartas Coringa em branco e Troca Tática ao mesmo tempo!",
        "icon": "☢️",
    },
    "haiku_boost": {
        "id": "haiku_boost",
        "name": "Sobrevivência & Combos",
        "desc": "Prioriza cartas de escolha dupla/tripla (Compre 2, Jogue 3) e troca tática liberada.",
        "icon": "🔥",
    },
}

AVATARS = ["💀", "😈", "🤡", "👽", "🦊", "🐙", "🤖", "🔥", "⚡", "👑", "🦄", "🐸", "🦇", "🐍", "💣", "🥃"]


def generate_room_code(existing_codes):
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(200):
        code = "".join(random.choices(chars, k=5))
        if code not in existing_codes:
            return code
    return "".join(random.choices(chars, k=6))


class Player:
    def __init__(self, name, sid, avatar=None, token=None, is_bot=False):
        self.token = token or str(uuid.uuid4())
        self.sid = sid
        self.name = name.strip()[:18] or "Anônimo"
        self.avatar = avatar or random.choice(AVATARS)
        self.hand = []
        self.score = 0
        self.connected = True
        self.is_bot = is_bot
        self.free_mulligan_used = False
        self.last_seen = time.time()

    def to_public_dict(self, is_host=False, is_judge=False, has_submitted=False, has_voted=False):
        return {
            "id": self.token,
            "name": self.name,
            "avatar": self.avatar,
            "score": self.score,
            "connected": self.connected,
            "is_bot": self.is_bot,
            "is_host": is_host,
            "is_judge": is_judge,
            "has_submitted": has_submitted,
            "has_voted": has_voted,
            "free_mulligan_used": self.free_mulligan_used,
        }


class FDPRoom:
    MAX_PLAYERS = 12
    MIN_PLAYERS = 3
    HAND_SIZE = 10

    def __init__(self, code, host_player):
        self.code = code
        self.created_at = time.time()
        self.last_activity = time.time()
        self.players = {host_player.token: host_player}
        self.player_order = [host_player.token]
        self.host_token = host_player.token

        # Configurações da Sala
        self.config = {
            "mode": "classico",
            "max_score": 7,
            "decks": list(DECK_METADATA.keys()),
            "wildcards_enabled": True,
            "timer_seconds": 60,
            "allow_reboot": True,
            "max_players": 12,
        }

        # Estado da Partida
        self.phase = "lobby"  # lobby | playing | judging | voting | round_result | game_over
        self.round_number = 0
        self.judge_index = 0
        self.judge_token = None

        self.white_deck = []
        self.white_discard = []
        self.black_deck = []
        self.black_discard = []

        self.current_black_card = None
        # Lista de jogadas da rodada:
        # [{"entry_id": str, "player_token": str, "cards": [str], "revealed": bool, "is_bet": bool, "votes": [token]}]
        self.submissions = []
        self.bet_pot = 0
        self.player_bet_used = set()
        self.votes_cast = {}  # voter_token -> entry_id

        self.round_winner_info = None
        self.game_winner_info = None
        self.history = []  # Melhores momentos da partida

        # Cronômetro
        self.timer_remaining = 0
        self.timer_active = False

        # Bot Rando Cardrissian
        self.rando_player = None

    def touch(self):
        self.last_activity = time.time()

    def human_tokens(self):
        return [t for t in self.player_order if t in self.players and not self.players[t].is_bot]

    def active_human_tokens(self):
        return [t for t in self.human_tokens() if self.players[t].connected]

    def add_player(self, name, sid, avatar=None, token=None):
        self.touch()
        # Reconexão por token
        if token and token in self.players:
            p = self.players[token]
            p.sid = sid
            p.connected = True
            p.last_seen = time.time()
            if name and name.strip():
                p.name = name.strip()[:18]
            if avatar:
                p.avatar = avatar
            return p, True, None

        if len(self.human_tokens()) >= self.config["max_players"]:
            return None, False, f"Sala cheia! Limite de {self.config['max_players']} jogadores atingido."

        # Evitar nomes duplicados
        base_name = (name.strip()[:18] or "Jogador")
        existing_names = {p.name.lower() for p in self.players.values()}
        final_name = base_name
        suffix = 2
        while final_name.lower() in existing_names:
            final_name = f"{base_name[:15]} {suffix}"
            suffix += 1

        new_player = Player(final_name, sid, avatar=avatar, token=token)
        self.players[new_player.token] = new_player
        self.player_order.append(new_player.token)

        # Se entrou no meio da partida, já compra cartas para participar da próxima (ou atual)
        if self.phase != "lobby":
            self._refill_hand(new_player)

        return new_player, False, None

    def disconnect_player(self, sid):
        for token, p in self.players.items():
            if p.sid == sid:
                p.connected = False
                p.last_seen = time.time()
                return token
        return None

    def remove_player(self, token):
        if token not in self.players or self.players[token].is_bot:
            return False
        p = self.players.pop(token)
        self.white_discard.extend([c for c in p.hand if c != WILDCARD_TOKEN])
        if token in self.player_order:
            self.player_order.remove(token)

        # Transferir host se necessário
        if self.host_token == token:
            humans = self.human_tokens()
            self.host_token = humans[0] if humans else None

        # Se o juiz saiu durante a rodada, avança ou redefine
        if self.phase in ("playing", "judging") and self.judge_token == token:
            humans = self.human_tokens()
            if len(humans) < self.MIN_PLAYERS:
                self.phase = "lobby"
                self.timer_active = False
            else:
                self.start_next_round(advance_judge=True)
        elif self.phase == "playing":
            self._check_all_submitted()
        elif self.phase == "voting":
            self._check_all_voted()
        return True

    def update_config(self, requester_token, new_config):
        if requester_token != self.host_token or self.phase != "lobby":
            return False, "Apenas o anfitrião pode alterar configurações no Lobby."

        mode = new_config.get("mode", self.config["mode"])
        if mode in GAME_MODES:
            self.config["mode"] = mode

        max_score = int(new_config.get("max_score", self.config["max_score"]))
        self.config["max_score"] = max(3, min(20, max_score))

        timer_sec = int(new_config.get("timer_seconds", self.config["timer_seconds"]))
        self.config["timer_seconds"] = max(0, min(180, timer_sec))

        decks = new_config.get("decks", self.config["decks"])
        valid_decks = [d for d in decks if d in DECK_METADATA]
        if valid_decks:
            self.config["decks"] = valid_decks

        self.config["wildcards_enabled"] = bool(new_config.get("wildcards_enabled", self.config["wildcards_enabled"]))
        self.config["allow_reboot"] = bool(new_config.get("allow_reboot", self.config["allow_reboot"]))

        # Ajustes automáticos de presets de modo
        if self.config["mode"] == "relampago":
            self.config["timer_seconds"] = 25
        elif self.config["mode"] == "caos_total":
            self.config["wildcards_enabled"] = True
            self.config["allow_reboot"] = True

        self.touch()
        return True, None

    def _build_decks(self):
        self.white_deck = []
        self.white_discard = []
        self.black_deck = []
        self.black_discard = []

        for d in self.config["decks"]:
            self.white_deck.extend(WHITE_DECKS.get(d, []))
            self.black_deck.extend(BLACK_DECKS.get(d, []))

        # Remover duplicatas exatas mantendo aleatoriedade
        self.white_deck = list(dict.fromkeys(self.white_deck))

        if self.config["wildcards_enabled"] or self.config["mode"] == "caos_total":
            # Adiciona 18 cartas Coringa (Escreva a Sua) espalhadas no baralho
            self.white_deck.extend([WILDCARD_TOKEN] * 18)

        random.shuffle(self.white_deck)

        if self.config["mode"] == "haiku_boost":
            multi = [c for c in self.black_deck if c["pick"] >= 2]
            single = [c for c in self.black_deck if c["pick"] == 1]
            random.shuffle(multi)
            random.shuffle(single)
            # Intercala para dar mais destaque às cartas duplas/triplas
            self.black_deck = []
            while multi or single:
                if multi:
                    self.black_deck.append(multi.pop())
                if single:
                    self.black_deck.append(single.pop())
                if single:
                    self.black_deck.append(single.pop())
        else:
            random.shuffle(self.black_deck)

    def _draw_white(self):
        if not self.white_deck:
            if not self.white_discard:
                for d in self.config["decks"]:
                    self.white_deck.extend(WHITE_DECKS.get(d, []))
            else:
                self.white_deck = self.white_discard[:]
                self.white_discard = []
            random.shuffle(self.white_deck)
        return self.white_deck.pop()

    def _draw_black(self):
        if not self.black_deck:
            self.black_deck = self.black_discard[:]
            self.black_discard = []
            random.shuffle(self.black_deck)
        return self.black_deck.pop()

    def _refill_hand(self, player, target_size=None):
        size = target_size or self.HAND_SIZE
        while len(player.hand) < size:
            player.hand.append(self._draw_white())

    def _uses_rando(self):
        return self.config["mode"] in ("rando", "caos_total")

    def _uses_gambling(self):
        return self.config["mode"] in ("aposta", "caos_total")

    def _is_democracy(self):
        return self.config["mode"] == "democracia"

    def start_game(self, requester_token):
        if requester_token != self.host_token:
            return False, "Apenas o anfitrião pode iniciar a partida."

        humans = self.human_tokens()
        min_req = 2 if (self._uses_rando() or self._is_democracy()) else self.MIN_PLAYERS
        if len(humans) < min_req:
            return False, f"É necessário pelo menos {min_req} jogadores para este modo."

        self._build_decks()
        self.round_number = 0
        self.judge_index = -1
        self.history = []
        self.game_winner_info = None

        # Remover Rando anterior se existir
        if self.rando_player and self.rando_player.token in self.players:
            del self.players[self.rando_player.token]
            self.rando_player = None

        # Resetar jogadores humanos
        for t in humans:
            p = self.players[t]
            p.score = 0
            p.hand = []
            p.free_mulligan_used = False
            self._refill_hand(p)

        # Adicionar Rando Cardrissian se ativo
        if self._uses_rando():
            self.rando_player = Player("Rando Cardrissian", sid=None, avatar="🤖", token="BOT_RANDO", is_bot=True)
            self.players["BOT_RANDO"] = self.rando_player

        self.start_next_round(advance_judge=True)
        return True, None

    def start_next_round(self, advance_judge=True, forced_judge_token=None):
        self.touch()
        self.round_number += 1
        self.submissions = []
        self.bet_pot = 0
        self.player_bet_used = set()
        self.votes_cast = {}
        self.round_winner_info = None

        humans = self.human_tokens()
        if not humans:
            self.phase = "lobby"
            self.timer_active = False
            return

        if self._is_democracy():
            self.judge_token = None
        elif forced_judge_token and forced_judge_token in humans:
            self.judge_token = forced_judge_token
            self.judge_index = humans.index(forced_judge_token)
        else:
            if advance_judge:
                self.judge_index = (self.judge_index + 1) % len(humans)
            else:
                self.judge_index = self.judge_index % len(humans)
            self.judge_token = humans[self.judge_index]

        if self.current_black_card:
            self.black_discard.append(self.current_black_card)
        self.current_black_card = self._draw_black()

        # Regra Oficial FDP/CAH: "Compre 2, Jogue 3" quando pick == 3 (e +1 quando pick == 2)
        pick = self.current_black_card["pick"]
        extra_draw = 2 if pick >= 3 else (1 if pick == 2 else 0)
        for t in humans:
            p = self.players[t]
            self._refill_hand(p, self.HAND_SIZE + extra_draw)

        # Rando joga imediatamente em segredo
        if self._uses_rando() and self.rando_player:
            rando_cards = []
            for _ in range(pick):
                c = self._draw_white()
                while c == WILDCARD_TOKEN:
                    c = self._draw_white()
                rando_cards.append(c)
            self.submissions.append({
                "entry_id": str(uuid.uuid4())[:8],
                "player_token": "BOT_RANDO",
                "cards": rando_cards,
                "revealed": False,
                "is_bet": False,
                "votes": [],
            })

        self.phase = "playing"
        self._start_timer(self.config["timer_seconds"])

    def _start_timer(self, seconds):
        if seconds and seconds > 0:
            self.timer_remaining = seconds
            self.timer_active = True
        else:
            self.timer_remaining = 0
            self.timer_active = False

    def _expected_submitters(self):
        return [
            t for t in self.active_human_tokens()
            if t != self.judge_token
        ]

    def _has_player_submitted(self, token):
        return any(s["player_token"] == token and not s["is_bet"] for s in self.submissions)

    def submit_cards(self, player_token, card_indices, custom_texts=None, is_bet=False):
        self.touch()
        if self.phase != "playing":
            return False, "Não é hora de jogar cartas."
        if player_token not in self.players:
            return False, "Jogador não encontrado."
        if player_token == self.judge_token:
            return False, "O Juiz da rodada não joga cartas!"

        player = self.players[player_token]
        pick = self.current_black_card["pick"]

        if is_bet:
            if not self._uses_gambling():
                return False, "Apostas não estão ativadas neste modo."
            if not self._has_player_submitted(player_token):
                return False, "Envie sua jogada principal antes de apostar."
            if player_token in self.player_bet_used:
                return False, "Você já apostou nesta rodada."
            if player.score < 1:
                return False, "Você precisa de pelo menos 1 ponto para apostar!"
        else:
            if self._has_player_submitted(player_token):
                return False, "Você já enviou sua jogada nesta rodada."

        if not isinstance(card_indices, list) or len(card_indices) != pick:
            return False, f"Selecione exatamente {pick} carta(s) na ordem correta."

        if len(set(card_indices)) != len(card_indices):
            return False, "Não é possível selecionar a mesma carta duas vezes."

        custom_texts = custom_texts or {}
        chosen_cards = []
        for idx in card_indices:
            if not isinstance(idx, int) or idx < 0 or idx >= len(player.hand):
                return False, "Índice de carta inválido."
            raw_card = player.hand[idx]
            if raw_card == WILDCARD_TOKEN:
                txt = str(custom_texts.get(str(idx), "")).strip()[:120]
                if not txt:
                    return False, "Escreva o texto da sua Carta Coringa antes de enviar!"
                chosen_cards.append(f"✨ {txt}")
            else:
                chosen_cards.append(raw_card)

        # Remover cartas da mão (do maior índice para o menor)
        for idx in sorted(card_indices, reverse=True):
            removed = player.hand.pop(idx)
            if removed != WILDCARD_TOKEN:
                self.white_discard.append(removed)

        if is_bet:
            player.score -= 1
            self.bet_pot += 1
            self.player_bet_used.add(player_token)

        self.submissions.append({
            "entry_id": str(uuid.uuid4())[:8],
            "player_token": player_token,
            "cards": chosen_cards,
            "revealed": False,
            "is_bet": is_bet,
            "votes": [],
        })

        self._check_all_submitted()
        return True, None

    def _check_all_submitted(self):
        expected = self._expected_submitters()
        if not expected:
            return
        if all(self._has_player_submitted(t) for t in expected):
            self._move_to_judging_or_voting()

    def _move_to_judging_or_voting(self):
        random.shuffle(self.submissions)
        if self._is_democracy():
            # Na democracia todas são reveladas para votação simultânea
            for s in self.submissions:
                s["revealed"] = True
            self.phase = "voting"
            self._start_timer(max(30, self.config["timer_seconds"]) if self.config["timer_seconds"] else 0)
        else:
            self.phase = "judging"
            self._start_timer(self.config["timer_seconds"] if self.config["mode"] == "relampago" else 0)

    def reveal_submission(self, requester_token, entry_id):
        if self.phase != "judging":
            return False, "Fase inválida."
        if requester_token != self.judge_token and requester_token != self.host_token:
            return False, "Apenas o Juiz pode revelar as cartas."
        for s in self.submissions:
            if s["entry_id"] == entry_id:
                s["revealed"] = True
                return True, None
        return False, "Jogada não encontrada."

    def reveal_all_submissions(self, requester_token):
        if self.phase != "judging":
            return False, "Fase inválida."
        if requester_token != self.judge_token and requester_token != self.host_token:
            return False, "Apenas o Juiz pode revelar todas as cartas."
        for s in self.submissions:
            s["revealed"] = True
        return True, None

    def judge_pick_winner(self, requester_token, entry_id):
        self.touch()
        if self.phase != "judging":
            return False, "Não estamos na fase de julgamento."
        if requester_token != self.judge_token and requester_token != self.host_token:
            return False, "Apenas o Juiz da rodada pode escolher a vencedora!"

        winner_entry = next((s for s in self.submissions if s["entry_id"] == entry_id), None)
        if not winner_entry:
            return False, "Jogada não encontrada."

        for s in self.submissions:
            s["revealed"] = True

        self._finalize_round([winner_entry])
        return True, None

    def cast_vote(self, voter_token, entry_id):
        self.touch()
        if self.phase != "voting":
            return False, "Não estamos em votação."
        if voter_token not in self.players:
            return False, "Jogador inválido."
        if voter_token in self.votes_cast:
            return False, "Você já votou nesta rodada."

        entry = next((s for s in self.submissions if s["entry_id"] == entry_id), None)
        if not entry:
            return False, "Jogada não encontrada."
        if entry["player_token"] == voter_token:
            return False, "Sem narcisismo! Você não pode votar na sua própria carta."

        self.votes_cast[voter_token] = entry_id
        entry["votes"].append(voter_token)
        self._check_all_voted()
        return True, None

    def _check_all_voted(self):
        voters = self.active_human_tokens()
        if voters and all(t in self.votes_cast for t in voters):
            self._resolve_democracy_round()

    def _resolve_democracy_round(self):
        if not self.submissions:
            self.start_next_round()
            return
        max_votes = max(len(s["votes"]) for s in self.submissions)
        winners = [s for s in self.submissions if len(s["votes"]) == max_votes]
        self._finalize_round(winners)

    def _finalize_round(self, winner_entries):
        self.timer_active = False
        points_awarded = 1 + self.bet_pot
        winners_payload = []

        for entry in winner_entries:
            w_token = entry["player_token"]
            w_player = self.players.get(w_token)
            if w_player:
                w_player.score += points_awarded
                winners_payload.append({
                    "player_id": w_token,
                    "player_name": w_player.name,
                    "player_avatar": w_player.avatar,
                    "is_bot": w_player.is_bot,
                    "cards": entry["cards"],
                    "entry_id": entry["entry_id"],
                    "points_gained": points_awarded,
                    "votes_count": len(entry["votes"]),
                })

        self.round_winner_info = {
            "black_card": self.current_black_card,
            "winners": winners_payload,
            "pot_bonus": self.bet_pot,
            "round_number": self.round_number,
        }

        if winners_payload:
            self.history.insert(0, {
                "round": self.round_number,
                "black": self.current_black_card["text"],
                "cards": winners_payload[0]["cards"],
                "winner": winners_payload[0]["player_name"],
                "avatar": winners_payload[0]["player_avatar"],
            })
            self.history = self.history[:15]

        # Checar condição de vitória da partida
        max_score = self.config["max_score"]
        champion = None
        for p in self.players.values():
            if p.score >= max_score:
                if not champion or p.score > champion.score:
                    champion = p

        if champion:
            self.phase = "game_over"
            ranking = sorted(self.players.values(), key=lambda x: x.score, reverse=True)
            self.game_winner_info = {
                "winner_name": champion.name,
                "winner_avatar": champion.avatar,
                "winner_score": champion.score,
                "is_rando_victory": champion.is_bot,
                "ranking": [
                    {"name": p.name, "avatar": p.avatar, "score": p.score, "is_bot": p.is_bot}
                    for p in ranking
                ],
            }
        else:
            self.phase = "round_result"

    def reboot_hand(self, player_token, card_indices, use_free=False):
        """Regra oficial Rebooting the Universe: trocar cartas gastando 1 ponto (ou 1 troca grátis por jogo)."""
        if not self.config["allow_reboot"]:
            return False, "Troca Tática está desativada nesta sala."
        if self.phase != "playing":
            return False, "Você só pode trocar cartas durante a fase de escolha."
        player = self.players.get(player_token)
        if not player:
            return False, "Jogador não encontrado."
        if not card_indices:
            return False, "Selecione ao menos uma carta para trocar."

        if use_free:
            if player.free_mulligan_used:
                return False, "Você já usou sua troca gratuita nesta partida!"
            player.free_mulligan_used = True
        else:
            if player.score < 1:
                return False, "Você precisa de 1 ponto para fazer Troca Tática."
            player.score -= 1

        valid_idxs = sorted(set(i for i in card_indices if 0 <= i < len(player.hand)), reverse=True)
        for idx in valid_idxs:
            removed = player.hand.pop(idx)
            if removed != WILDCARD_TOKEN:
                self.white_discard.append(removed)
        self._refill_hand(player, len(player.hand) + len(valid_idxs))
        return True, None

    def next_round_trigger(self, requester_token):
        if self.phase != "round_result":
            return False, "A rodada ainda não terminou."
        if requester_token != self.host_token and requester_token != self.judge_token:
            return False, "Apenas o Anfitrião ou o Juiz podem avançar para a próxima rodada."

        # Modo Reinado do FDP: vencedor humano vira o próximo Juiz
        forced_judge = None
        if self.config["mode"] == "reinado" and self.round_winner_info:
            winners = self.round_winner_info.get("winners", [])
            human_winners = [w for w in winners if not w["is_bot"]]
            if human_winners:
                forced_judge = human_winners[0]["player_id"]

        self.start_next_round(advance_judge=(forced_judge is None), forced_judge_token=forced_judge)
        return True, None

    def force_advance(self, requester_token):
        """Permite ao Host destravar a rodada caso alguém fique AFK."""
        if requester_token != self.host_token:
            return False, "Apenas o anfitrião pode forçar o avanço."
        if self.phase == "playing":
            self._auto_submit_missing()
            return True, None
        elif self.phase == "judging":
            if self.submissions:
                random_entry = random.choice(self.submissions)
                self._finalize_round([random_entry])
            else:
                self.start_next_round()
            return True, None
        elif self.phase == "voting":
            self._resolve_democracy_round()
            return True, None
        elif self.phase == "round_result":
            return self.next_round_trigger(requester_token)
        return False, "Nada para avançar."

    def _auto_submit_missing(self):
        pick = self.current_black_card["pick"]
        for t in self._expected_submitters():
            if not self._has_player_submitted(t):
                p = self.players[t]
                if len(p.hand) < pick:
                    self._refill_hand(p, self.HAND_SIZE)
                idxs = list(range(min(pick, len(p.hand))))
                custom = {}
                for i in idxs:
                    if p.hand[i] == WILDCARD_TOKEN:
                        custom[str(i)] = "Dormiu no ponto e jogou no automático."
                self.submit_cards(t, idxs, custom_texts=custom)
        if self.phase == "playing":
            self._move_to_judging_or_voting()

    def tick_timer(self):
        """Chamado a cada 1 segundo pelo background task."""
        if not self.timer_active or self.timer_remaining <= 0:
            return False
        self.timer_remaining -= 1
        if self.timer_remaining <= 0:
            self.timer_active = False
            if self.phase == "playing":
                self._auto_submit_missing()
                return True
            elif self.phase == "judging" and self.submissions:
                self._finalize_round([random.choice(self.submissions)])
                return True
            elif self.phase == "voting":
                self._resolve_democracy_round()
                return True
        return False

    def reset_to_lobby(self, requester_token):
        if requester_token != self.host_token:
            return False, "Apenas o anfitrião pode voltar ao Lobby."
        self.phase = "lobby"
        self.timer_active = False
        self.submissions = []
        self.current_black_card = None
        self.round_winner_info = None
        self.game_winner_info = None
        if self.rando_player and "BOT_RANDO" in self.players:
            del self.players["BOT_RANDO"]
            self.rando_player = None
        return True, None

    def get_state_for_player(self, viewer_token):
        viewer = self.players.get(viewer_token)
        players_list = []
        ordered_tokens = list(self.player_order)
        if self.rando_player and "BOT_RANDO" in self.players:
            ordered_tokens.append("BOT_RANDO")

        for t in ordered_tokens:
            if t not in self.players:
                continue
            p = self.players[t]
            players_list.append(
                p.to_public_dict(
                    is_host=(t == self.host_token),
                    is_judge=(t == self.judge_token),
                    has_submitted=self._has_player_submitted(t),
                    has_voted=(t in self.votes_cast),
                )
            )

        # Sanitizar submissões para manter anonimato até o resultado
        public_submissions = []
        show_authors = self.phase in ("round_result", "game_over")
        for s in self.submissions:
            author = self.players.get(s["player_token"])
            is_mine = (s["player_token"] == viewer_token)
            public_submissions.append({
                "entry_id": s["entry_id"],
                "cards": s["cards"] if (s["revealed"] or show_authors) else [],
                "revealed": s["revealed"] or show_authors,
                "is_bet": s["is_bet"],
                "is_mine": is_mine,
                "votes_count": len(s["votes"]) if show_authors else None,
                "author_name": author.name if (show_authors and author) else None,
                "author_avatar": author.avatar if (show_authors and author) else None,
            })

        judge_player = self.players.get(self.judge_token)

        return {
            "room_code": self.code,
            "phase": self.phase,
            "round_number": self.round_number,
            "config": self.config,
            "players": players_list,
            "host_id": self.host_token,
            "judge_id": self.judge_token,
            "judge_name": judge_player.name if judge_player else None,
            "judge_avatar": judge_player.avatar if judge_player else None,
            "current_black_card": self.current_black_card,
            "submissions": public_submissions,
            "submissions_count": len(self.submissions),
            "expected_submissions": len(self._expected_submitters()) + (1 if self._uses_rando() else 0),
            "bet_pot": self.bet_pot,
            "timer_remaining": self.timer_remaining,
            "timer_active": self.timer_active,
            "round_winner": self.round_winner_info,
            "game_winner": self.game_winner_info,
            "history": self.history,
            "me": {
                "id": viewer.token if viewer else None,
                "name": viewer.name if viewer else None,
                "avatar": viewer.avatar if viewer else None,
                "score": viewer.score if viewer else 0,
                "hand": viewer.hand if viewer else [],
                "is_host": (viewer_token == self.host_token),
                "is_judge": (viewer_token == self.judge_token),
                "has_submitted": self._has_player_submitted(viewer_token),
                "can_bet": (
                    self._uses_gambling()
                    and self.phase == "playing"
                    and viewer_token != self.judge_token
                    and self._has_player_submitted(viewer_token)
                    and viewer_token not in self.player_bet_used
                    and viewer is not None
                    and viewer.score >= 1
                ),
                "has_voted": (viewer_token in self.votes_cast),
                "free_mulligan_used": viewer.free_mulligan_used if viewer else True,
            },
        }
