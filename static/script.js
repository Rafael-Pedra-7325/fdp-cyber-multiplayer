(() => {
  const socket = io({
    transports: ["polling", "websocket"],
    reconnection: true,
    reconnectionAttempts: Infinity,
    reconnectionDelay: 1000,
    reconnectionDelayMax: 5000,
  });

  // ==================== ESTADO DO CLIENTE ====================
  let metaData = null;
  let state = null;
  let selectedAvatar = localStorage.getItem("fdp_avatar") || "😈";
  let myToken = localStorage.getItem("fdp_token") || null;
  let savedRoom = localStorage.getItem("fdp_room") || null;
  let pendingUrlSala = new URLSearchParams(window.location.search).get("sala");
  let reconnectPending = false;
  let connectErrorShown = false;

  let selectedIndices = [];       // Ordem das cartas selecionadas para jogar
  let customWildTexts = {};       // index -> texto customizado
  let editingWildIdx = null;
  let rebootMode = false;         // Modo de troca tática de mão
  let betMode = false;            // Modo de aposta (2ª jogada)
  let soundEnabled = localStorage.getItem("fdp_sound") !== "off";

  // ==================== SINTETIZADOR WEB AUDIO ====================
  let audioCtx = null;
  function playSound(type) {
    if (!soundEnabled) return;
    try {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === "suspended") audioCtx.resume();
      const now = audioCtx.currentTime;
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.connect(gain);
      gain.connect(audioCtx.destination);

      if (type === "card") {
        osc.type = "triangle";
        osc.frequency.setValueAtTime(520, now);
        osc.frequency.exponentialRampToValueAtTime(220, now + 0.08);
        gain.gain.setValueAtTime(0.14, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.09);
        osc.start(now);
        osc.stop(now + 0.09);
      } else if (type === "reveal") {
        osc.type = "sine";
        osc.frequency.setValueAtTime(340, now);
        osc.frequency.exponentialRampToValueAtTime(680, now + 0.14);
        gain.gain.setValueAtTime(0.15, now);
        gain.gain.exponentialRampToValueAtTime(0.001, now + 0.16);
        osc.start(now);
        osc.stop(now + 0.16);
      } else if (type === "start") {
        [330, 440, 660].forEach((freq, i) => {
          const o = audioCtx.createOscillator();
          const g = audioCtx.createGain();
          o.type = "triangle";
          o.frequency.value = freq;
          o.connect(g); g.connect(audioCtx.destination);
          g.gain.setValueAtTime(0.12, now + i * 0.07);
          g.gain.exponentialRampToValueAtTime(0.001, now + i * 0.07 + 0.18);
          o.start(now + i * 0.07);
          o.stop(now + i * 0.07 + 0.18);
        });
      } else if (type === "win") {
        [440, 554, 659, 880].forEach((freq, i) => {
          const o = audioCtx.createOscillator();
          const g = audioCtx.createGain();
          o.type = "sine";
          o.frequency.value = freq;
          o.connect(g); g.connect(audioCtx.destination);
          g.gain.setValueAtTime(0.16, now + i * 0.09);
          g.gain.exponentialRampToValueAtTime(0.001, now + i * 0.09 + 0.28);
          o.start(now + i * 0.09);
          o.stop(now + i * 0.09 + 0.28);
        });
      }
    } catch (_) {}
  }

  // ==================== HELPERS DOM ====================
  const $ = (id) => document.getElementById(id);

  function showView(viewId) {
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    $(viewId).classList.add("active");
    const inRoom = viewId !== "view-home";
    $("room-header-pill").classList.toggle("hidden", !inRoom);
    $("btn-leave").classList.toggle("hidden", !inRoom);
    $("btn-history").style.display = (viewId === "view-game" || viewId === "view-gameover") ? "inline-flex" : "none";
  }

  function showToast(msg, isError = false) {
    const el = document.createElement("div");
    el.className = "toast" + (isError ? " error" : "");
    el.textContent = msg;
    $("toast-container").appendChild(el);
    setTimeout(() => el.remove(), 3600);
  }

  function escapeHtml(str) {
    return String(str || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function getInviteUrl() {
    const code = state ? state.room_code : "";
    return `${window.location.origin}/?sala=${code}`;
  }

  function copyInviteLink() {
    if (!state) return;
    const url = getInviteUrl();
    navigator.clipboard.writeText(url)
      .then(() => showToast("🔗 Link da sala copiado! Mande no grupo."))
      .catch(() => showToast(`Código da sala: ${state.room_code}`));
  }

  function shareWhatsApp() {
    if (!state) return;
    const url = getInviteUrl();
    const text = encodeURIComponent(`🔥 Bora jogar FDP (Foi De Propósito) +18!\nEntra na sala ${state.room_code} pelo celular:\n${url}`);
    window.open(`https://wa.me/?text=${text}`, "_blank");
  }

  function openQRModal() {
    if (!state) return;
    const url = encodeURIComponent(getInviteUrl());
    $("qr-room-code").textContent = state.room_code;
    $("qr-image").src = `https://api.qrserver.com/v1/create-qr-code/?size=240x240&data=${url}`;
    $("modal-qr").classList.remove("hidden");
  }

  function tryAutoReconnect() {
    if (reconnectPending) return;
    const roomToRejoin = state?.room_code || (!pendingUrlSala ? savedRoom : null);
    if (roomToRejoin && myToken) {
      reconnectPending = true;
      socket.emit("join_room_request", {
        room_code: roomToRejoin,
        name: localStorage.getItem("fdp_name") || $("input-name").value.trim() || "Jogador",
        avatar: selectedAvatar,
        token: myToken,
        auto_reconnect: true,
      });
    }
  }

  // ==================== INICIALIZAÇÃO ====================
  async function loadMeta() {
    try {
      const res = await fetch("/api/meta");
      if (!res.ok) throw new Error("HTTP " + res.status);
      metaData = await res.json();
      $("meta-total-cards").textContent = metaData.deck_stats.grand_total.toLocaleString("pt-BR");
      renderAvatarGrid(metaData.avatars);
      if (state) renderState();
    } catch (e) {
      showToast("Conectando ao servidor... aguarde alguns segundos.", true);
    }
  }

  async function init() {
    const savedName = localStorage.getItem("fdp_name");
    if (savedName) $("input-name").value = savedName;

    if (pendingUrlSala) $("input-room-code").value = pendingUrlSala.toUpperCase();

    $("btn-sound").textContent = soundEnabled ? "🔊" : "🔇";

    await loadMeta();

    if (socket.connected && !state) {
      tryAutoReconnect();
    }
  }

  function renderAvatarGrid(avatars) {
    const grid = $("avatar-grid");
    grid.innerHTML = "";
    avatars.forEach((av) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "avatar-opt" + (av === selectedAvatar ? " selected" : "");
      btn.textContent = av;
      btn.onclick = () => {
        selectedAvatar = av;
        localStorage.setItem("fdp_avatar", av);
        document.querySelectorAll(".avatar-opt").forEach((b) => b.classList.remove("selected"));
        btn.classList.add("selected");
      };
      grid.appendChild(btn);
    });
  }

  // ==================== AÇÕES HOME & LOBBY ====================
  $("btn-create-room").onclick = () => {
    const name = $("input-name").value.trim() || "Anfitrião";
    localStorage.setItem("fdp_name", name);
    socket.emit("create_room", { name, avatar: selectedAvatar, token: myToken });
  };

  $("btn-join-room").onclick = () => {
    const code = $("input-room-code").value.trim().toUpperCase();
    if (!code) return showToast("Digite o código da sala!", true);
    const name = $("input-name").value.trim() || "Jogador";
    localStorage.setItem("fdp_name", name);
    socket.emit("join_room_request", { room_code: code, name, avatar: selectedAvatar, token: myToken });
  };

  $("btn-leave").onclick = () => {
    if (!confirm("Deseja realmente sair desta sala?")) return;
    socket.emit("leave_room_request");
    localStorage.removeItem("fdp_room");
    savedRoom = null;
    state = null;
    reconnectPending = false;
    showView("view-home");
  };

  $("btn-copy-link").onclick = copyInviteLink;
  $("btn-lobby-copy").onclick = copyInviteLink;
  $("btn-share-wa").onclick = shareWhatsApp;
  $("btn-lobby-wa").onclick = shareWhatsApp;
  $("btn-lobby-qr").onclick = openQRModal;
  $("btn-close-qr").onclick = () => $("modal-qr").classList.add("hidden");

  $("btn-rules").onclick = () => $("modal-rules").classList.remove("hidden");
  $("btn-close-rules").onclick = () => $("modal-rules").classList.add("hidden");
  $("btn-history").onclick = () => {
    renderHistoryModal();
    $("modal-history").classList.remove("hidden");
  };
  $("btn-close-history").onclick = () => $("modal-history").classList.add("hidden");

  $("btn-sound").onclick = () => {
    soundEnabled = !soundEnabled;
    localStorage.setItem("fdp_sound", soundEnabled ? "on" : "off");
    $("btn-sound").textContent = soundEnabled ? "🔊" : "🔇";
  };

  // Configurações do Host
  function pushConfig(partial) {
    if (!state || !state.me.is_host) return;
    socket.emit("update_config", { ...state.config, ...partial });
  }

  $("cfg-max-score").oninput = (e) => { $("lbl-max-score").textContent = e.target.value; };
  $("cfg-max-score").onchange = (e) => pushConfig({ max_score: Number(e.target.value) });

  $("cfg-timer").oninput = (e) => {
    const v = Number(e.target.value);
    $("lbl-timer").textContent = v === 0 ? "Sem limite" : `${v}s`;
  };
  $("cfg-timer").onchange = (e) => pushConfig({ timer_seconds: Number(e.target.value) });

  $("cfg-wildcards").onchange = (e) => pushConfig({ wildcards_enabled: e.target.checked });
  $("cfg-reboot").onchange = (e) => pushConfig({ allow_reboot: e.target.checked });

  $("btn-start-game").onclick = () => socket.emit("start_game");
  $("btn-play-again").onclick = () => socket.emit("start_game");
  $("btn-back-lobby").onclick = () => socket.emit("return_lobby");
  $("btn-next-round").onclick = () => socket.emit("next_round");
  $("btn-reveal-all").onclick = () => socket.emit("reveal_all");
  $("btn-force-advance").onclick = () => socket.emit("force_advance");

  // Reações
  document.querySelectorAll(".react-btn").forEach((btn) => {
    btn.onclick = () => socket.emit("send_reaction", { emoji: btn.dataset.emoji });
  });

  // Modal Coringa
  $("btn-wildcard-cancel").onclick = () => {
    editingWildIdx = null;
    $("modal-wildcard").classList.add("hidden");
  };
  $("btn-wildcard-save").onclick = () => {
    const txt = $("wildcard-input").value.trim();
    if (!txt) return showToast("Escreva algum absurdo primeiro!", true);
    customWildTexts[String(editingWildIdx)] = txt;
    if (!selectedIndices.includes(editingWildIdx)) {
      const pick = state.current_black_card?.pick || 1;
      if (selectedIndices.length >= pick) selectedIndices.shift();
      selectedIndices.push(editingWildIdx);
    }
    editingWildIdx = null;
    $("modal-wildcard").classList.add("hidden");
    renderHand();
  };

  // Botões da Mão
  $("btn-reboot-mode").onclick = () => {
    rebootMode = !rebootMode;
    betMode = false;
    selectedIndices = [];
    renderHand();
  };

  $("btn-bet-mode").onclick = () => {
    betMode = !betMode;
    rebootMode = false;
    selectedIndices = [];
    renderHand();
  };

  $("btn-submit-cards").onclick = () => {
    if (!state) return;
    if (rebootMode) {
      if (!selectedIndices.length) return;
      const useFree = !state.me.free_mulligan_used;
      socket.emit("reboot_hand", { indices: selectedIndices, use_free: useFree });
      rebootMode = false;
      selectedIndices = [];
      return;
    }

    socket.emit("submit_cards", {
      indices: selectedIndices,
      custom_texts: customWildTexts,
      is_bet: betMode,
    });
    selectedIndices = [];
    customWildTexts = {};
    betMode = false;
  };

  // ==================== RENDERIZAÇÃO DE TELAS ====================
  function renderState() {
    if (!state) return;
    $("header-room-code").textContent = state.room_code;

    if (state.phase === "lobby") {
      showView("view-lobby");
      renderLobby();
    } else if (state.phase === "game_over") {
      showView("view-gameover");
      renderGameOver();
    } else {
      showView("view-game");
      renderGame();
    }
  }

  function renderLobby() {
    const humans = state.players.filter((p) => !p.is_bot);
    $("lobby-count").textContent = humans.length;
    $("lobby-code-big").textContent = state.room_code;

    const list = $("lobby-players-list");
    list.innerHTML = "";
    humans.forEach((p) => {
      const div = document.createElement("div");
      div.className = "player-tile" + (p.connected ? "" : " offline");
      div.innerHTML = `
        <div class="player-tile-left">
          <span style="font-size:1.35rem">${escapeHtml(p.avatar)}</span>
          <div>
            <div class="player-tile-name">${escapeHtml(p.name)} ${p.id === state.me.id ? "(Você)" : ""}</div>
            ${p.is_host ? '<div class="host-crown">👑 Anfitrião</div>' : ""}
          </div>
        </div>
        ${state.me.is_host && !p.is_host ? `<button class="kick-btn" title="Remover">✖</button>` : ""}
      `;
      const kickBtn = div.querySelector(".kick-btn");
      if (kickBtn) {
        kickBtn.onclick = () => socket.emit("kick_player", { player_id: p.id });
      }
      list.appendChild(div);
    });

    const isHost = state.me.is_host;
    $("host-start-area").classList.toggle("hidden", !isHost);
    $("guest-wait-area").classList.toggle("hidden", isHost);
    $("config-lock-badge").textContent = isHost ? "VOCÊ CONTROLA" : "SOMENTE LEITURA";

    // Render Modos
    if (metaData) {
      const mGrid = $("modes-grid");
      mGrid.innerHTML = "";
      Object.values(metaData.game_modes).forEach((m) => {
        const card = document.createElement("button");
        card.type = "button";
        card.className = "mode-card" + (state.config.mode === m.id ? " active" : "");
        card.disabled = !isHost;
        card.innerHTML = `
          <div class="mode-card-title">${m.icon} ${m.name}</div>
          <div class="mode-card-desc">${m.desc}</div>
        `;
        card.onclick = () => pushConfig({ mode: m.id });
        mGrid.appendChild(card);
      });

      // Render Expansões
      const dGrid = $("decks-grid");
      dGrid.innerHTML = "";
      let selectedTotal = 0;
      Object.values(metaData.deck_stats.decks).forEach((d) => {
        const active = state.config.decks.includes(d.id);
        if (active) selectedTotal += d.total;
        const el = document.createElement("div");
        el.className = "deck-chip" + (active ? " active" : "");
        el.innerHTML = `
          <div class="deck-chip-top">
            <span>${d.name}</span>
            <span class="deck-tag" style="color:${d.color}">${d.tag}</span>
          </div>
          <div class="deck-counts">${d.white_count} brancas • ${d.black_count} pretas</div>
        `;
        if (isHost) {
          el.onclick = () => {
            let next = [...state.config.decks];
            if (next.includes(d.id)) {
              if (next.length <= 1) return showToast("Mantenha pelo menos 1 baralho ativo!", true);
              next = next.filter((x) => x !== d.id);
            } else {
              next.push(d.id);
            }
            pushConfig({ decks: next });
          };
        }
        dGrid.appendChild(el);
      });
      $("selected-cards-total").textContent = selectedTotal.toLocaleString("pt-BR");
    }

    // Sliders e Toggles
    $("cfg-max-score").value = state.config.max_score;
    $("cfg-max-score").disabled = !isHost;
    $("lbl-max-score").textContent = state.config.max_score;

    $("cfg-timer").value = state.config.timer_seconds;
    $("cfg-timer").disabled = !isHost || state.config.mode === "relampago";
    $("lbl-timer").textContent = state.config.timer_seconds === 0 ? "Sem limite" : `${state.config.timer_seconds}s`;

    $("cfg-wildcards").checked = state.config.wildcards_enabled;
    $("cfg-wildcards").disabled = !isHost;
    $("cfg-reboot").checked = state.config.allow_reboot;
    $("cfg-reboot").disabled = !isHost;
  }

  function renderGame() {
    // 1. Placar
    const strip = $("scoreboard-strip");
    strip.innerHTML = "";
    state.players.forEach((p) => {
      const pill = document.createElement("div");
      pill.className = "score-pill"
        + (p.is_judge ? " is-judge" : "")
        + (p.has_submitted && state.phase === "playing" ? " submitted" : "")
        + (!p.connected && !p.is_bot ? " offline" : "")
        + (p.id === state.me.id ? " is-me" : "");

      let statusIcon = "";
      if (p.is_judge) statusIcon = "👑";
      else if (state.phase === "playing") statusIcon = p.has_submitted ? "✅" : "⏳";
      else if (state.phase === "voting") statusIcon = p.has_voted ? "🗳️" : "⏳";

      pill.innerHTML = `
        <span>${escapeHtml(p.avatar)}</span>
        <strong>${escapeHtml(p.name)}</strong>
        <span class="score-num">${p.score}</span>
        <span class="status-badge-sm">${statusIcon}</span>
      `;
      strip.appendChild(pill);
    });

    // 2. Banner de Rodada
    $("game-round-num").textContent = state.round_number;
    const modeInfo = metaData?.game_modes?.[state.config.mode];
    $("game-mode-badge").textContent = modeInfo ? `${modeInfo.icon} ${modeInfo.name.toUpperCase()}` : state.config.mode;
    $("game-pot-badge").classList.toggle("hidden", state.bet_pot <= 0);
    $("game-pot-badge").textContent = `🎰 POTE: +${state.bet_pot} PT`;
    $("btn-force-advance").classList.toggle("hidden", !state.me.is_host);

    updateTimerUI(state.timer_remaining, state.timer_active);

    // Mensagem de Status Dinâmica
    let statusMsg = "";
    if (state.phase === "playing") {
      if (state.me.is_judge) statusMsg = "👑 Você é o JUIZ desta rodada! Relaxe enquanto eles escolhem.";
      else if (state.me.has_submitted) statusMsg = "✅ Jogada enviada! Aguardando o resto da mesa...";
      else statusMsg = `🃏 Escolha ${state.current_black_card.pick} carta(s) na sua mão!`;
    } else if (state.phase === "judging") {
      statusMsg = state.me.is_judge
        ? "⚖️ SUA VEZ, JUIZ! Revele as cartas e clique na mais FDP!"
        : `⚖️ ${state.judge_avatar} ${state.judge_name} está julgando as respostas...`;
    } else if (state.phase === "voting") {
      statusMsg = state.me.has_voted
        ? "🗳️ Voto computado! Aguardando os demais..."
        : "🗳️ DEMOCRACIA CAÓTICA! Vote na melhor combinação (exceto a sua)!";
    } else if (state.phase === "round_result") {
      statusMsg = "🏆 Rodada finalizada! Confira os autores reais abaixo.";
    }
    $("round-status-text").textContent = statusMsg;

    // 3. Carta Preta
    const bc = state.current_black_card;
    if (bc) {
      $("black-pick-badge").textContent = bc.pick === 1 ? "ESCOLHA 1" : `ESCOLHA ${bc.pick}`;
      const formattedText = escapeHtml(bc.text).replace(/_{3,}/g, '<span class="blank-slot">     </span>');
      $("black-card-text").innerHTML = formattedText;
      $("judge-label-card").textContent = state.judge_name
        ? `👑 Juiz: ${state.judge_avatar} ${state.judge_name}`
        : "🗳️ Modo Democracia (Sem Juiz)";
      $("submission-progress").textContent = `${state.submissions_count}/${state.expected_submissions} jogadas`;
    }

    // 4. Mesa de Submissões
    renderTableZone();

    // 5. Mão do Jogador
    renderHand();
  }

  function updateTimerUI(remaining, active) {
    const box = $("game-timer-box");
    box.classList.toggle("hidden", !active);
    $("game-timer-val").textContent = remaining;
    box.classList.toggle("urgent", active && remaining <= 10);
  }

  function renderTableZone() {
    const grid = $("submissions-grid");
    const banner = $("round-winner-banner");
    const canJudge = state.phase === "judging" && (state.me.is_judge || state.me.is_host);
    const hasHidden = state.submissions.some((s) => !s.revealed);

    $("btn-reveal-all").classList.toggle("hidden", !(canJudge && hasHidden));
    $("btn-next-round").classList.toggle("hidden", !(state.phase === "round_result" && (state.me.is_host || state.me.is_judge)));

    // Banner Vencedor da Rodada
    if (state.phase === "round_result" && state.round_winner) {
      const winners = state.round_winner.winners;
      const names = winners.map((w) => `${escapeHtml(w.player_avatar)} ${escapeHtml(w.player_name)}`).join(" & ");
      const pts = winners[0]?.points_gained || 1;
      banner.classList.remove("hidden");
      banner.innerHTML = `
        <div>
          <div style="font-size:0.75rem;color:var(--neon-gold);font-weight:800;letter-spacing:1px;">🏆 VENCEDOR DA RODADA</div>
          <div style="font-size:1.15rem;font-weight:800;margin-top:2px;">${names} levou +${pts} ponto(s)!</div>
        </div>
        <div style="font-size:0.85rem;color:#fff;font-weight:700;">"${escapeHtml(winners[0]?.cards.join(" / ") || "")}"</div>
      `;
    } else {
      banner.classList.add("hidden");
    }

    grid.innerHTML = "";
    if (!state.submissions.length) {
      grid.innerHTML = `<div style="grid-column:1/-1;text-align:center;color:var(--text-muted);padding:40px 10px;">Nenhuma carta jogada ainda... aguardando a baixaria começar.</div>`;
      return;
    }

    const winnerIds = new Set((state.round_winner?.winners || []).map((w) => w.entry_id));

    state.submissions.forEach((sub, idx) => {
      const box = document.createElement("div");
      const isWinner = winnerIds.has(sub.entry_id);
      box.className = "submission-bundle"
        + (!sub.revealed ? " face-down" : "")
        + (isWinner ? " is-winner" : "");

      if (!sub.revealed) {
        if (canJudge) box.classList.add("clickable");
        box.innerHTML = `
          <div class="face-down-label">
            <span class="face-down-icon">🃏</span>
            Jogada #${idx + 1} ${sub.is_bet ? "🎰" : ""}<br/>
            <small>${canJudge ? "Clique para revelar" : "Secreta"}</small>
          </div>
        `;
        if (canJudge) {
          box.onclick = () => socket.emit("reveal_submission", { entry_id: sub.entry_id });
        }
      } else {
        const cardsHtml = sub.cards.map((c) => `<div class="sub-white-card">${escapeHtml(c)}</div>`).join("");
        let footerHtml = "";

        if (state.phase === "round_result") {
          footerHtml = `
            <div class="sub-footer">
              <span>${escapeHtml(sub.author_avatar || "")} <strong>${escapeHtml(sub.author_name || "")}</strong></span>
              <span>${sub.votes_count != null && state.config.mode === "democracia" ? `🗳️ ${sub.votes_count}` : ""} ${isWinner ? "🏆" : ""}</span>
            </div>
          `;
        } else if (state.phase === "judging" && state.me.is_judge) {
          footerHtml = `<button class="btn btn-primary pick-winner-btn">👑 ESCOLHER VENCEDORA</button>`;
        } else if (state.phase === "voting" && !state.me.has_voted && !sub.is_mine) {
          footerHtml = `<button class="btn btn-cyan pick-winner-btn">🗳️ VOTAR NESTA</button>`;
        } else if (sub.is_mine) {
          footerHtml = `<div class="sub-footer"><span>Sua jogada ${sub.is_bet ? "🎰" : ""}</span></div>`;
        }

        box.innerHTML = cardsHtml + footerHtml;

        const actionBtn = box.querySelector(".pick-winner-btn");
        if (actionBtn) {
          actionBtn.onclick = () => {
            if (state.phase === "judging") socket.emit("pick_winner", { entry_id: sub.entry_id });
            else if (state.phase === "voting") socket.emit("cast_vote", { entry_id: sub.entry_id });
          };
        }
      }

      grid.appendChild(box);
    });
  }

  function renderHand() {
    const handEl = $("player-hand");
    const me = state.me;
    const pick = state.current_black_card?.pick || 1;
    const canPlayNormal = state.phase === "playing" && !me.is_judge && !me.has_submitted;
    const canPlayBet = betMode && me.can_bet;
    const canInteract = canPlayNormal || canPlayBet || rebootMode;

    $("hand-count").textContent = me.hand.length;

    // Botão Troca Tática (Reboot)
    const showReboot = state.config.allow_reboot && state.phase === "playing" && (me.score >= 1 || !me.free_mulligan_used);
    const rebootBtn = $("btn-reboot-mode");
    rebootBtn.classList.toggle("hidden", !showReboot);
    rebootBtn.classList.toggle("active-mode", rebootMode);
    rebootBtn.textContent = rebootMode
      ? "✖ Cancelar Troca"
      : (!me.free_mulligan_used ? "🔄 Troca Grátis (1x)" : "🔄 Trocar Cartas (-1pt)");

    // Botão Aposta Arriscada
    const betBtn = $("btn-bet-mode");
    betBtn.classList.toggle("hidden", !me.can_bet);
    betBtn.textContent = betMode ? "✖ Cancelar Aposta" : "🎰 Apostar 1pt (+1 Jogada)";

    // Texto de ajuda + Botão Enviar
    const submitBtn = $("btn-submit-cards");
    if (rebootMode) {
      $("hand-hint").textContent = "Toque nas cartas que deseja descartar e comprar novas.";
      submitBtn.textContent = `🔄 TROCAR (${selectedIndices.length})`;
      submitBtn.disabled = selectedIndices.length === 0;
    } else if (me.is_judge) {
      $("hand-hint").textContent = "👑 Você é o Juiz desta rodada — suas cartas estão descansando.";
      submitBtn.textContent = "VOCÊ É O JUIZ";
      submitBtn.disabled = true;
    } else if (me.has_submitted && !betMode) {
      $("hand-hint").textContent = "✅ Sua jogada já está na mesa!";
      submitBtn.textContent = "ENVIADO ✅";
      submitBtn.disabled = true;
    } else {
      $("hand-hint").textContent = betMode
        ? `🎰 APOSTA ATIVA! Escolha mais ${pick} carta(s) para sua 2ª jogada.`
        : `Selecione ${pick} carta(s) na ordem correta (${selectedIndices.length}/${pick}).`;
      submitBtn.textContent = betMode ? "🎰 ENVIAR APOSTA (-1 PT)" : "🚀 ENVIAR JOGADA";
      submitBtn.disabled = !canInteract || selectedIndices.length !== pick;
    }

    handEl.innerHTML = "";
    me.hand.forEach((cardText, idx) => {
      const isWild = cardText === "__WILDCARD__";
      const orderPos = selectedIndices.indexOf(idx);
      const isSelected = orderPos !== -1;

      const card = document.createElement("div");
      card.className = "white-card"
        + (isWild ? " wildcard" : "")
        + (isSelected ? (rebootMode ? " reboot-selected" : " selected") : "")
        + (!canInteract ? " disabled" : "");

      const displayContent = isWild
        ? (customWildTexts[String(idx)] ? `✨ ${customWildTexts[String(idx)]}` : "✨ CARTA CORINGA\n(Toque para escrever sua própria resposta +18)")
        : cardText;

      card.innerHTML = `
        ${isSelected && !rebootMode ? `<div class="card-order-badge">${orderPos + 1}</div>` : ""}
        <div>${escapeHtml(displayContent).replace(/\n/g, "<br/>")}</div>
        <div class="white-card-foot">
          <span>FDP // +18</span>
          <span>${isWild ? "EDITÁVEL" : "#" + (idx + 1)}</span>
        </div>
      `;

      if (canInteract) {
        card.onclick = () => {
          playSound("card");
          if (rebootMode) {
            if (isSelected) selectedIndices = selectedIndices.filter((i) => i !== idx);
            else selectedIndices.push(idx);
            renderHand();
            return;
          }

          if (isWild && !isSelected) {
            editingWildIdx = idx;
            $("wildcard-input").value = customWildTexts[String(idx)] || "";
            $("modal-wildcard").classList.remove("hidden");
            $("wildcard-input").focus();
            return;
          }

          if (isSelected) {
            selectedIndices = selectedIndices.filter((i) => i !== idx);
          } else {
            if (selectedIndices.length >= pick) {
              selectedIndices.shift();
            }
            selectedIndices.push(idx);
          }
          renderHand();
        };
      }

      handEl.appendChild(card);
    });
  }

  function renderGameOver() {
    const gw = state.game_winner;
    if (!gw) return;
    $("gameover-emoji").textContent = gw.is_rando_victory ? "🤖💀" : "🏆👑";
    $("gameover-subtitle").textContent = gw.is_rando_victory ? "HUMILHAÇÃO HUMANA TOTAL" : "CAMPEÃO SUPREMO DO FDP";
    $("gameover-title").textContent = `${gw.winner_avatar} ${gw.winner_name}`;
    $("gameover-roast").textContent = gw.is_rando_victory
      ? "Vocês conseguiram perder para um bot sorteando cartas aleatórias. Que vergonha!"
      : `Com ${gw.winner_score} pontos de pura baixaria e zero arrependimento.`;

    const list = $("gameover-ranking");
    list.innerHTML = "";
    gw.ranking.forEach((r, i) => {
      const row = document.createElement("div");
      row.className = "rank-row" + (i === 0 ? " top1" : "");
      row.innerHTML = `
        <span><strong>#${i + 1}</strong> ${escapeHtml(r.avatar)} ${escapeHtml(r.name)}</span>
        <span class="score-num">${r.score} pts</span>
      `;
      list.appendChild(row);
    });

    const isHost = state.me.is_host;
    $("btn-play-again").classList.toggle("hidden", !isHost);
    $("btn-back-lobby").classList.toggle("hidden", !isHost);
    $("gameover-guest-wait").classList.toggle("hidden", isHost);
  }

  function renderHistoryModal() {
    const list = $("history-list");
    if (!state || !state.history.length) {
      list.innerHTML = `<p style="color:var(--text-muted);text-align:center;">Nenhuma rodada finalizada ainda.</p>`;
      return;
    }
    list.innerHTML = state.history.map((h) => `
      <div class="history-item">
        <div class="history-black">Rodada ${h.round}: ${escapeHtml(h.black)}</div>
        <div class="history-white">➜ ${escapeHtml(h.cards.join(" / "))}</div>
        <div class="history-who">🏆 ${escapeHtml(h.avatar)} ${escapeHtml(h.winner)}</div>
      </div>
    `).join("");
  }

  function spawnFloatingReaction(emoji, sender) {
    const el = document.createElement("div");
    el.className = "float-emoji";
    el.style.left = `${10 + Math.random() * 80}%`;
    el.innerHTML = `<span>${escapeHtml(emoji)}</span><span class="float-sender">${escapeHtml(sender)}</span>`;
    $("reactions-layer").appendChild(el);
    setTimeout(() => el.remove(), 2700);
  }

  // ==================== LISTENERS SOCKET.IO ====================
  socket.on("connect", () => {
    connectErrorShown = false;
    if (!metaData) loadMeta();
    tryAutoReconnect();
  });

  socket.on("disconnect", () => {
    reconnectPending = false;
  });

  socket.on("connect_error", () => {
    reconnectPending = false;
    if (!connectErrorShown) {
      connectErrorShown = true;
      showToast("Conectando ao servidor... aguarde alguns segundos.", true);
    }
  });

  socket.on("joined_success", (data) => {
    reconnectPending = false;
    myToken = data.token;
    savedRoom = data.room_code;
    pendingUrlSala = null;
    localStorage.setItem("fdp_token", myToken);
    localStorage.setItem("fdp_room", data.room_code);
    if (window.location.search) {
      window.history.replaceState({}, document.title, window.location.pathname);
    }
  });

  socket.on("session_expired", () => {
    reconnectPending = false;
    localStorage.removeItem("fdp_room");
    savedRoom = null;
    state = null;
    showView("view-home");
  });

  socket.on("room_state", (newState) => {
    const prevRound = state?.round_number;
    const prevPhase = state?.phase;
    state = newState;

    if (prevRound !== state.round_number || prevPhase !== state.phase) {
      selectedIndices = [];
      customWildTexts = {};
      rebootMode = false;
      betMode = false;
    }
    renderState();
  });

  socket.on("timer_tick", (data) => {
    if (state) {
      state.timer_remaining = data.remaining;
      state.timer_active = data.active;
      updateTimerUI(data.remaining, data.active);
    }
  });

  socket.on("toast", (data) => showToast(data.message));
  socket.on("error_msg", (data) => {
    reconnectPending = false;
    showToast(data.message, true);
  });
  socket.on("sound_cue", (data) => playSound(data.sound));
  socket.on("floating_reaction", (data) => spawnFloatingReaction(data.emoji, data.sender));

  socket.on("kicked", (data) => {
    reconnectPending = false;
    showToast(data.message, true);
    localStorage.removeItem("fdp_room");
    savedRoom = null;
    state = null;
    showView("view-home");
  });

  // Keep-alive para evitar hibernação do Render Free durante partidas ativas
  setInterval(() => {
    if (state && state.room_code) {
      fetch("/health", { cache: "no-store" }).catch(() => {});
    }
  }, 240000);

  init();
})();
