/**
 * SMF SHOP — Interactive Cursor-Following & Ambient Grid Illumination
 * 
 * Implements:
 * 1. Full-viewport continuous coverage across Storefront and Customer Portal.
 * 2. Smooth cursor-following spotlight (blend: royal blue -> cyan highlight -> violet edge).
 * 3. Localized illuminated grid lines around the cursor.
 * 4. Ambient gentle drift for mobile devices and idle states.
 * 5. Full preservation of the existing rich royal blue / indigo / violet base palette.
 */
(function initSMFCursorLight() {
  if (typeof window === "undefined") return;

  function setup() {
    // 1. Ensure .bg container exists and has all 3 required layers
    let bg = document.querySelector(".bg");
    if (!bg) {
      bg = document.createElement("div");
      bg.className = "bg";
      bg.setAttribute("aria-hidden", "true");
      document.body.prepend(bg);
    }

    if (!bg.querySelector(".bg-grid")) {
      const grid = document.createElement("div");
      grid.className = "bg-grid";
      bg.appendChild(grid);
    }

    if (!bg.querySelector(".bg-spotlight")) {
      const spot = document.createElement("div");
      spot.className = "bg-spotlight";
      bg.appendChild(spot);
    }

    if (!bg.querySelector(".bg-grid-glow")) {
      const glow = document.createElement("div");
      glow.className = "bg-grid-glow";
      bg.appendChild(glow);
    }

    // 2. Physics & Motion State
    let targetX = window.innerWidth * 0.5;
    let targetY = window.innerHeight * 0.35;
    let currentX = targetX;
    let currentY = targetY;
    
    let isUserInteracting = false;
    let idleTimeout = null;
    let animAngle = 0;
    let isTouch = ("ontouchstart" in window) || (navigator.maxTouchPoints > 0);

    function onPointer(x, y) {
      targetX = x;
      targetY = y;
      isUserInteracting = true;

      if (idleTimeout) clearTimeout(idleTimeout);
      // Resume slow ambient drift 2.8s after pointer stops
      idleTimeout = setTimeout(() => {
        isUserInteracting = false;
      }, 2800);
    }

    window.addEventListener("pointermove", (e) => {
      onPointer(e.clientX, e.clientY);
    }, { passive: true });

    window.addEventListener("touchmove", (e) => {
      if (e.touches && e.touches[0]) {
        onPointer(e.touches[0].clientX, e.touches[0].clientY);
      }
    }, { passive: true });

    window.addEventListener("touchstart", (e) => {
      if (e.touches && e.touches[0]) {
        onPointer(e.touches[0].clientX, e.touches[0].clientY);
      }
    }, { passive: true });

    // Initial variable set
    document.documentElement.style.setProperty("--cursor-x", `${currentX.toFixed(1)}px`);
    document.documentElement.style.setProperty("--cursor-y", `${currentY.toFixed(1)}px`);

    // 3. Silky 60/120fps Animation Loop
    function renderLoop() {
      if (!isUserInteracting) {
        // Very slow, soothing ambient illumination drift across the grid (ideal for mobile & idle)
        animAngle += 0.007;
        const w = window.innerWidth;
        const h = window.innerHeight;
        targetX = (w * 0.5) + Math.sin(animAngle * 0.6) * (w * 0.32);
        targetY = (h * 0.38) + Math.cos(animAngle * 0.8) * (h * 0.22);
      }

      // Smooth lerp (0.12) gives a natural, organic liquid light glide
      currentX += (targetX - currentX) * 0.12;
      currentY += (targetY - currentY) * 0.12;

      document.documentElement.style.setProperty("--cursor-x", `${currentX.toFixed(1)}px`);
      document.documentElement.style.setProperty("--cursor-y", `${currentY.toFixed(1)}px`);

      requestAnimationFrame(renderLoop);
    }

    requestAnimationFrame(renderLoop);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup);
  } else {
    setup();
  }
})();
