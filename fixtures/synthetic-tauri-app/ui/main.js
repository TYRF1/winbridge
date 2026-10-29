// The window intentionally shows nothing until the configured delay has
// elapsed. The delay lives in Rust so WINBRIDGE_FIXTURE_DELAY_MS reaches the
// app through the environment Wine launches it with.
const FALLBACK_DELAY_MS = 2000;

async function fixtureDelayMs() {
  try {
    const value = await window.__TAURI__.core.invoke("fixture_delay_ms");
    return Number.isFinite(value) && value >= 0 ? value : FALLBACK_DELAY_MS;
  } catch (error) {
    console.error("fixture_delay_ms failed; using the fallback delay", error);
    return FALLBACK_DELAY_MS;
  }
}

window.addEventListener("DOMContentLoaded", async () => {
  const delayMs = await fixtureDelayMs();
  await new Promise((resolve) => setTimeout(resolve, delayMs));

  document.getElementById("delay-value").textContent = `${delayMs} ms`;
  document.body.classList.remove("loading");
  document.body.classList.add("ready");
  document.getElementById("app").setAttribute("aria-busy", "false");
});
