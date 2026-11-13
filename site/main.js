/* Orbo site — small behaviours only.
 *
 * Two things are NOT connected yet. Fill these in before launch:
 *   CONFIG.depositUrl        Checkout link for the $199 refundable deposit on Orbo (for example a Stripe Payment Link).
 *   CONFIG.waitlistEndpoint  A URL that accepts POST { email } as JSON.
 * Until then, the buttons tell the visitor honestly that the step is not live.
 * Orbo Open needs neither: its button links straight to the GitHub repo.
 */
const CONFIG = {
  depositUrl: "",
  waitlistEndpoint: "",
};

/* Nav: transparent over the hero, gains a surface once the page scrolls. */
const nav = document.querySelector("[data-nav]");
if (nav) {
  const setStuck = () => nav.classList.toggle("is-stuck", window.scrollY > 24);
  setStuck();
  window.addEventListener("scroll", setStuck, { passive: true });
}

/* Hero: tap Orbo and it flaps its flippers, with a smile.
 *
 * Each flipper has one joint: a shaft that runs left to right through the shoulder.
 * So the flipper swings forward and back, like a pendulum. The page shows Orbo from
 * the front, so this code projects that swing onto the screen:
 *   - the flipper looks shorter as it swings away from vertical,
 *   - its middle rises towards the shoulder,
 *   - it looks slightly larger when it is forward (nearer) and smaller when it is back.
 * The angle follows one smooth curve that starts and ends at rest, with no stops between.
 */
const SWING = {
  duration: 2000,   // ms, whole gesture
  cycles: 3,        // forward-and-back swings inside the gesture
  amplitude: 36,    // degrees, at the middle of the gesture
  pivot: 0.4,       // shoulder-to-middle distance, as a share of flipper length
  thickness: 0.5,   // flipper depth as a share of its length (it is a pod, not a card)
  distance: 3.5,    // viewer distance, in flipper lengths (sets the size change)
};
const waver = document.querySelector("[data-wave]");
if (waver) {
  const tap = waver.querySelector(".orbo__tap");
  const flippers = [...waver.querySelectorAll(".flipper")].map((el) => ({
    el,
    // flipper length as a share of its image height; the image also holds the shadow margin
    core: parseFloat(getComputedStyle(el).getPropertyValue("--core")) || 1,
  }));
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)");

  const pose = (degrees) => {
    const angle = (degrees * Math.PI) / 180;
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    const lift = -SWING.pivot * (1 - cos) * 100;                       // % of flipper length
    const short = Math.sqrt(cos * cos + SWING.thickness ** 2 * sin * sin);
    const near = 1 / (1 - (SWING.pivot * sin) / SWING.distance);
    const light = 1 + 0.05 * sin;
    for (const { el, core } of flippers) {
      el.style.transform = `translateY(${(lift * core).toFixed(3)}%) scale(${near.toFixed(4)}, ${(near * short).toFixed(4)})`;
      el.style.filter = `brightness(${light.toFixed(3)})`;
    }
  };

  tap.addEventListener("click", () => {
    if (waver.classList.contains("is-waving")) return;
    waver.classList.add("is-waving", "has-waved");
    waver.dataset.mood = "happy";
    const duration = calm.matches ? 700 : SWING.duration;
    const cycles = calm.matches ? 1 : SWING.cycles;
    const amplitude = calm.matches ? 16 : SWING.amplitude;
    const start = performance.now();
    const frame = (now) => {
      const u = Math.min(Math.max((now - start) / duration, 0), 1);
      // A sine swing inside a sine envelope: starts from rest, builds, then settles back to rest.
      pose(amplitude * Math.sin(2 * Math.PI * cycles * u) * Math.sin(Math.PI * u));
      if (u < 1) {
        requestAnimationFrame(frame);
        return;
      }
      for (const { el } of flippers) {
        el.style.transform = "";
        el.style.filter = "";
      }
      waver.classList.remove("is-waving");
      waver.dataset.mood = "calm";
    };
    requestAnimationFrame(frame);
  });
}

/* Mood picker: recolours the ring and swaps the eyes on the labelled Orbo. */
const MOOD_CAPTIONS = {
  calm: "Calm. A steady aqua glow, and slow blinks.",
  curious: "Curious. The ring turns violet and one eye opens wide.",
  happy: "Happy. A warm yellow ring, and eyes that smile.",
  affection: "Fond. The ring blushes pink. So do the eyes.",
};
const moodTarget = document.querySelector("[data-mood-target]");
const moodCaption = document.querySelector("[data-mood-caption]");
const moodButtons = [...document.querySelectorAll(".mood")];
moodButtons.forEach((button) => {
  button.addEventListener("click", () => {
    const mood = button.dataset.mood;
    moodButtons.forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    if (moodTarget) moodTarget.dataset.mood = mood;
    if (moodCaption) moodCaption.textContent = MOOD_CAPTIONS[mood];
  });
});

/* Puff swatches: tint the puff on the two robots in the dress section. */
const puffTarget = document.querySelector("[data-puff-target]");
const swatches = [...document.querySelectorAll(".swatch")];
swatches.forEach((button) => {
  button.addEventListener("click", () => {
    swatches.forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
    if (puffTarget) puffTarget.dataset.puff = button.dataset.puff;
  });
});

/* Deposit button (Orbo only). */
const depositStatus = document.querySelector("[data-deposit-status]");
document.querySelectorAll("[data-deposit]").forEach((link) => {
  if (CONFIG.depositUrl) {
    link.href = CONFIG.depositUrl;
    return;
  }
  link.addEventListener("click", (event) => {
    event.preventDefault();
    if (!depositStatus) return;
    depositStatus.hidden = false;
    depositStatus.textContent =
      "Deposit checkout is not open yet. Leave your email below and we will write the moment it is.";
  });
});

/* Waitlist form. */
const form = document.querySelector("[data-waitlist-form]");
if (form) {
  const input = form.querySelector(".join__input");
  const button = form.querySelector("button[type=submit]");
  const label = form.querySelector(".btn__label");
  const message = form.querySelector(".join__msg");
  const setState = (state, text) => {
    form.classList.toggle("is-error", state === "error");
    form.classList.toggle("is-done", state === "done");
    message.textContent = text || "";
    input.setAttribute("aria-invalid", String(state === "error"));
  };

  input.addEventListener("input", () => {
    if (form.classList.contains("is-error")) setState("idle");
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const email = input.value.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) {
      setState("error", "That email does not look right. Check it and try again.");
      input.focus();
      return;
    }

    if (!CONFIG.waitlistEndpoint) {
      setState("error", "The waitlist is not connected yet, so nothing was saved. Please check back soon.");
      return;
    }

    button.disabled = true;
    button.classList.add("is-loading");
    label.textContent = "Joining…";
    try {
      const response = await fetch(CONFIG.waitlistEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      if (!response.ok) throw new Error(String(response.status));
      setState("done", "You are on the list. We will write when there is news.");
      input.disabled = true;
      label.textContent = "Joined";
    } catch {
      setState("error", "That did not go through. Your email was not saved. Please try again.");
      button.disabled = false;
      label.textContent = "Join the waitlist";
    } finally {
      button.classList.remove("is-loading");
    }
  });
}
