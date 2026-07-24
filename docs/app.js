/* Fool Me Thrice — game logic (no build step, no dependencies) */

const SUPABASE_URL = "https://hiszyxiimtakqnrwzska.supabase.co";
const SUPABASE_KEY = "sb_publishable_geqZ-GgPqTfe4bxzZIT0_A_aoUHe5Ey"; // public read-only key
const SHARE_URL = location.origin + location.pathname;
const LIVES = 3;

const $ = (id) => document.getElementById(id);

const state = {
  deck: [],        // cards for this run, pre-mixed
  index: 0,
  score: 0,
  wrong: 0,
  best: Number(localStorage.getItem("fmt-best") || 0),
  answering: false,
};

/* ---------------------------------------------------------- data */

async function fetchCards() {
  const url =
    `${SUPABASE_URL}/rest/v1/cards` +
    `?select=id,verdict,claim,category,explanation,source_url&active=eq.true`;
  const resp = await fetch(url, { headers: { apikey: SUPABASE_KEY } });
  if (!resp.ok) throw new Error(`Supabase ${resp.status}`);
  const rows = await resp.json();
  if (!rows.length) throw new Error("no cards in database");
  return rows;
}

function shuffle(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

/** Deal an exactly 50/50 REAL/FAKE deck so guessing one side blindly
 *  never beats the odds, then shuffle for an unpredictable order. */
function buildDeck(cards) {
  const real = shuffle(cards.filter((c) => c.verdict === "REAL"));
  const fake = shuffle(cards.filter((c) => c.verdict === "FAKE"));
  const n = Math.min(real.length, fake.length);
  if (n === 0) return shuffle(cards.slice());
  return shuffle(real.slice(0, n).concat(fake.slice(0, n)));
}

/* ---------------------------------------------------------- UI helpers */

const CATEGORY_LABELS = {
  defence: "Defence & security",
  health: "Health",
  finance: "Money & finance",
  schemes: "Govt schemes",
  "jobs-education": "Jobs & education",
  tech: "Tech",
  misc: "News",
};

function show(screenId) {
  for (const s of ["screen-intro", "screen-game", "screen-over", "screen-about"]) {
    $(s).classList.toggle("hidden", s !== screenId);
  }
}

function renderHud() {
  $("hud-score").textContent = state.score;
  $("hud-lives").innerHTML = Array.from({ length: LIVES }, (_, i) =>
    `<span class="${i < state.wrong ? "lost" : ""}">❤️</span>`
  ).join("");
}

function cardAt(i) {
  return state.deck[i] || null;
}

function fillCard(el, card) {
  el.querySelector(".card-cat").textContent = card
    ? CATEGORY_LABELS[card.category] || card.category
    : "";
  el.querySelector(".card-claim").textContent = card ? card.claim : "";
}

function renderCards() {
  const card = $("card");
  card.classList.remove("flying", "spring");
  card.style.transform = "";
  card.style.opacity = "";
  setStampOpacity(0);
  fillCard(card, cardAt(state.index));
  fillCard($("card-next"), cardAt(state.index + 1));
}

function setStampOpacity(dx) {
  $("stamp-real").style.opacity = dx > 0 ? Math.min(dx / 90, 1) : 0;
  $("stamp-fake").style.opacity = dx < 0 ? Math.min(-dx / 90, 1) : 0;
}

/* ---------------------------------------------------------- game flow */

function startGame() {
  state.index = 0;
  state.score = 0;
  state.wrong = 0;
  state.answering = false;
  state.deck = buildDeck(state.allCards.slice());
  renderHud();
  renderCards();
  show("screen-game");
}

/** Anonymous fire-and-forget answer log for per-card difficulty stats.
 *  Harmless no-op until the answers table + insert policy exist. */
function logAnswer(card, guess, correct) {
  fetch(`${SUPABASE_URL}/rest/v1/answers`, {
    method: "POST",
    headers: {
      apikey: SUPABASE_KEY,
      "Content-Type": "application/json",
      Prefer: "return=minimal",
    },
    body: JSON.stringify({ card_id: card.id, guess, correct }),
    keepalive: true,
  }).catch(() => {});
}

function answer(guess, viaSwipe, dx) {
  if (state.answering) return;
  const card = cardAt(state.index);
  if (!card) return;
  state.answering = true;

  const correct = guess === card.verdict;
  logAnswer(card, guess, correct);
  if (correct) state.score += 1;
  else state.wrong += 1;
  renderHud();

  // fly the card out
  const el = $("card");
  const dir = guess === "REAL" ? 1 : -1;
  el.classList.add("flying");
  el.style.transform =
    `translate(${dir * (window.innerWidth + 200)}px, ${viaSwipe ? dx * 0.2 : -40}px) ` +
    `rotate(${dir * 22}deg)`;

  // reveal sheet
  const body = $("sheet-body");
  body.classList.toggle("good", correct);
  body.classList.toggle("bad", !correct);
  $("sheet-result").textContent = correct
    ? ["Nailed it!", "Sharp eye!", "Correct!"][Math.floor(Math.random() * 3)]
    : ["Fooled!", "Gotcha!", "Not quite!"][Math.floor(Math.random() * 3)];
  $("sheet-verdict").innerHTML =
    card.verdict === "FAKE"
      ? `This claim is <b class="v-fake">FAKE</b>`
      : `This one is <b class="v-real">REAL</b>`;
  $("sheet-explain").textContent = card.explanation;
  $("sheet-source").href = card.source_url;
  $("btn-next").textContent =
    state.wrong >= LIVES ? "See my result" : "Next card";
  setTimeout(() => $("sheet").classList.remove("hidden"), 250);
}

function nextCard() {
  $("sheet").classList.add("hidden");
  state.index += 1;
  state.answering = false;

  if (state.wrong >= LIVES || !cardAt(state.index)) {
    endGame();
    return;
  }
  renderCards();
}

function endGame() {
  const survived = state.wrong >= LIVES;
  if (state.score > state.best) {
    state.best = state.score;
    localStorage.setItem("fmt-best", String(state.best));
  }
  $("over-emoji").textContent = survived ? "💔" : "🏆";
  $("over-title").textContent = survived ? "Fooled thrice!" : "Deck cleared!";
  $("over-detail").textContent = survived
    ? `Misinformation got you after ${state.score} correct call${state.score === 1 ? "" : "s"}.`
    : "You judged every card in the deck. Impressive.";
  $("over-score").textContent = state.score;
  $("over-best").textContent = state.best;
  $("share-copied").classList.add("hidden");
  show("screen-over");
}

async function shareResult() {
  const text =
    `🕵️ Fool Me Thrice — I called ${state.score} India news claim${state.score === 1 ? "" : "s"} ` +
    `right before misinformation fooled me thrice. Can you tell real from fake?`;
  if (navigator.share) {
    try {
      await navigator.share({ text, url: SHARE_URL });
      return;
    } catch (e) {
      /* user cancelled — fall through to clipboard */
    }
  }
  try {
    await navigator.clipboard.writeText(`${text} ${SHARE_URL}`);
    $("share-copied").classList.remove("hidden");
  } catch (e) {
    prompt("Copy your result:", `${text} ${SHARE_URL}`);
  }
}

/* ---------------------------------------------------------- swipe input */

function initSwipe() {
  const el = $("card");
  let startX = 0, startY = 0, dx = 0, dy = 0, dragging = false;

  el.addEventListener("pointerdown", (e) => {
    if (state.answering) return;
    dragging = true;
    startX = e.clientX;
    startY = e.clientY;
    el.classList.remove("spring");
    el.setPointerCapture(e.pointerId);
  });

  el.addEventListener("pointermove", (e) => {
    if (!dragging || state.answering) return;
    dx = e.clientX - startX;
    dy = e.clientY - startY;
    el.style.transform =
      `translate(${dx}px, ${dy * 0.25}px) rotate(${dx * 0.06}deg)`;
    setStampOpacity(dx);
  });

  const release = () => {
    if (!dragging) return;
    dragging = false;
    if (Math.abs(dx) > 90 && !state.answering) {
      answer(dx > 0 ? "REAL" : "FAKE", true, dy);
    } else {
      el.classList.add("spring");
      el.style.transform = "";
      setStampOpacity(0);
    }
    dx = dy = 0;
  };
  el.addEventListener("pointerup", release);
  el.addEventListener("pointercancel", release);
}

/* ---------------------------------------------------------- boot */

async function boot() {
  renderHud();
  initSwipe();

  $("btn-start").addEventListener("click", startGame);
  $("btn-again").addEventListener("click", startGame);
  $("btn-about").addEventListener("click", () => show("screen-about"));
  $("btn-about-back").addEventListener("click", () => show("screen-intro"));
  $("btn-next").addEventListener("click", nextCard);
  $("btn-share").addEventListener("click", shareResult);
  $("btn-real").addEventListener("click", () => answer("REAL", false, 0));
  $("btn-fake").addEventListener("click", () => answer("FAKE", false, 0));

  document.addEventListener("keydown", (e) => {
    if ($("screen-game").classList.contains("hidden")) return;
    if (!$("sheet").classList.contains("hidden")) {
      if (e.key === "Enter" || e.key === " ") nextCard();
      return;
    }
    if (e.key === "ArrowRight") answer("REAL", false, 0);
    if (e.key === "ArrowLeft") answer("FAKE", false, 0);
  });

  try {
    state.allCards = await fetchCards();
    const btn = $("btn-start");
    btn.disabled = false;
    btn.textContent = `Start — ${state.allCards.length} claims loaded`;
  } catch (err) {
    const btn = $("btn-start");
    btn.textContent = "Couldn't load cards — tap to retry";
    btn.disabled = false;
    btn.onclick = () => location.reload();
    console.error(err);
  }
}

boot();
