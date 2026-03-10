import { useRef, useCallback, useEffect } from "react";
import { useDrag } from "@use-gesture/react";

export type SwipeDirection = "left" | "right" | "down" | null;

interface SpringState {
  x: number;
  y: number;
  vx: number;
  vy: number;
}

interface UseSwipeOptions {
  onSwipe: (direction: SwipeDirection) => void | Promise<void>;
  onSwipeThreshold?: (direction: SwipeDirection) => void;
  enabled?: boolean;
}

interface UseSwipeResult {
  bind: ReturnType<typeof useDrag>;
  cardRef: React.RefObject<HTMLDivElement | null>;
  exitingRef: React.RefObject<HTMLDivElement | null>;
  isAnimating: boolean;
}

const SWIPE_X_THRESHOLD = 0.3; // 30% of viewport width
const SWIPE_Y_THRESHOLD = 0.3; // 30% of viewport height
const VELOCITY_THRESHOLD = 0.5;
const AXIS_LOCK_PX = 10;

const SPRING_STIFFNESS = 0.15;
const SPRING_DAMPING = 0.7;

const EXIT_DISTANCE = 1.5; // multiplier of viewport dimension

function animateSpring(
  el: HTMLElement,
  initial: SpringState,
  target: { x: number; y: number },
  onComplete: () => void,
) {
  const state = { ...initial };
  let raf: number;
  const dt = 1;

  function step() {
    const dx = state.x - target.x;
    const dy = state.y - target.y;

    state.vx += -SPRING_STIFFNESS * dx - SPRING_DAMPING * state.vx;
    state.vy += -SPRING_STIFFNESS * dy - SPRING_DAMPING * state.vy;
    state.x += state.vx * dt;
    state.y += state.vy * dt;

    const rotation = state.x * 0.05;
    el.style.transform = `translate3d(${state.x}px, ${state.y}px, 0) rotate(${rotation}deg)`;

    const distToTarget = Math.sqrt(
      (state.x - target.x) ** 2 + (state.y - target.y) ** 2,
    );
    const speed = Math.sqrt(state.vx ** 2 + state.vy ** 2);

    if (distToTarget < 0.5 && speed < 0.5) {
      el.style.transform =
        target.x === 0 && target.y === 0
          ? "translate3d(0, 0, 0)"
          : `translate3d(${target.x}px, ${target.y}px, 0)`;
      onComplete();
      return;
    }

    raf = requestAnimationFrame(step);
  }

  raf = requestAnimationFrame(step);
  return () => cancelAnimationFrame(raf);
}

export function useSwipe({
  onSwipe,
  onSwipeThreshold,
  enabled = true,
}: UseSwipeOptions): UseSwipeResult {
  const cardRef = useRef<HTMLDivElement | null>(null);
  const exitingRef = useRef<HTMLDivElement | null>(null);
  const isAnimatingRef = useRef(false);
  const lockedAxisRef = useRef<"x" | "y" | null>(null);
  const cancelAnimRef = useRef<(() => void) | null>(null);
  const startedInScrollableRef = useRef(false);
  const scrollLastYRef = useRef<number | null>(null);

  const bind = useDrag(
    ({ down, movement: [mx, my], velocity: [vx, vy], cancel, first, last, event }) => {
      const el = cardRef.current;
      if (!el) return;

      if (!enabled || isAnimatingRef.current) {
        // Reset card position so it doesn't get stuck mid-drag
        el.style.transform = "translate3d(0, 0, 0)";
        cancel();
        return;
      }

      if (first) {
        lockedAxisRef.current = null;
        scrollLastYRef.current = null;
        // Check if the drag started inside a scrollable area (e.g. .hunk-card-body)
        // If so, suppress vertical swipe to allow native scrolling
        const target = event?.target as HTMLElement | null;
        startedInScrollableRef.current = !!target?.closest(".hunk-card-body");
      }

      // Axis locking
      if (lockedAxisRef.current === null) {
        if (Math.abs(mx) > AXIS_LOCK_PX || Math.abs(my) > AXIS_LOCK_PX) {
          lockedAxisRef.current = Math.abs(mx) > Math.abs(my) ? "x" : "y";
        }
      }

      // If the drag started in a scrollable area and the user is dragging
      // vertically, scroll the card body programmatically (since touch-action:none
      // on descendants prevents native scrolling).
      if (startedInScrollableRef.current && lockedAxisRef.current === "y") {
        const scrollBody = el.querySelector(".hunk-card-body") as HTMLElement | null;
        if (scrollBody) {
          scrollBody.scrollTop -= my - (scrollLastYRef.current ?? 0);
          scrollLastYRef.current = my;
        }
        if (last) {
          scrollLastYRef.current = null;
        }
        return;
      }

      const effectiveMx = lockedAxisRef.current === "y" ? 0 : mx;
      const effectiveMy = lockedAxisRef.current === "x" ? 0 : my;
      // Only allow downward movement
      const clampedMy = effectiveMy > 0 ? effectiveMy : 0;

      if (down && !last) {
        const rotation = effectiveMx * 0.05;
        el.style.transform = `translate3d(${effectiveMx}px, ${clampedMy}px, 0) rotate(${rotation}deg)`;
        return;
      }

      // Release — determine if swipe threshold met
      const vw = window.innerWidth;
      const vh = window.innerHeight;

      let direction: SwipeDirection = null;

      if (lockedAxisRef.current === "x" || lockedAxisRef.current === null) {
        if (effectiveMx > vw * SWIPE_X_THRESHOLD || (vx > VELOCITY_THRESHOLD && effectiveMx > 0)) {
          direction = "right";
        } else if (effectiveMx < -vw * SWIPE_X_THRESHOLD || (vx > VELOCITY_THRESHOLD && effectiveMx < 0)) {
          direction = "left";
        }
      }

      if (
        direction === null &&
        (lockedAxisRef.current === "y" || lockedAxisRef.current === null)
      ) {
        if (clampedMy > vh * SWIPE_Y_THRESHOLD || vy > VELOCITY_THRESHOLD) {
          direction = "down";
        }
      }

      isAnimatingRef.current = true;

      if (direction === null) {
        // Spring back to center
        cancelAnimRef.current = animateSpring(
          el,
          { x: effectiveMx, y: clampedMy, vx: 0, vy: 0 },
          { x: 0, y: 0 },
          () => {
            isAnimatingRef.current = false;
          },
        );
      } else {
        // Animate off-screen using the exiting layer.
        // 1. Copy the current card's innerHTML into the exiting element
        // 2. Position the exiting element where the card currently is
        // 3. Reset the real card immediately (it will show next card content)
        // 4. Animate the exiting element off-screen
        const exitEl = exitingRef.current;

        if (exitEl) {
          // Clone current card content into the exiting layer
          exitEl.innerHTML = el.innerHTML;
          exitEl.style.transform = `translate3d(${effectiveMx}px, ${clampedMy}px, 0) rotate(${effectiveMx * 0.05}deg)`;
          exitEl.style.display = "block";
        }

        // Reset the real card immediately — no flash because the exiting
        // layer is on top, visually covering the transition
        el.style.transform = "translate3d(0, 0, 0)";

        // Notify threshold reached so SwipeDeck can advance immediately
        onSwipeThreshold?.(direction);

        // Fire the swipe callback (advances index, does API calls)
        // This is intentionally not awaited — the exit animation is cosmetic
        void onSwipe(direction);

        if (exitEl) {
          const target = {
            x:
              direction === "right"
                ? vw * EXIT_DISTANCE
                : direction === "left"
                  ? -vw * EXIT_DISTANCE
                  : 0,
            y: direction === "down" ? vh * EXIT_DISTANCE : 0,
          };

          // @use-gesture v10 reports velocity as absolute values,
          // so we need to sign it based on swipe direction to avoid
          // the exit animation fighting the target direction.
          const signedVx =
            direction === "left" ? -vx * 100 :
            direction === "right" ? vx * 100 : 0;
          const signedVy = direction === "down" ? vy * 100 : 0;

          cancelAnimRef.current = animateSpring(
            exitEl,
            {
              x: effectiveMx,
              y: clampedMy,
              vx: signedVx,
              vy: signedVy,
            },
            target,
            () => {
              exitEl.style.display = "none";
              exitEl.innerHTML = "";
              isAnimatingRef.current = false;
            },
          );
        } else {
          // No exiting element — just finish
          isAnimatingRef.current = false;
        }
      }
    },
    {
      filterTaps: true,
      pointer: { touch: true, capture: false },
    },
  );

  // Prevent the browser from claiming touch events for its own gestures
  // (e.g. scroll, back-navigation in Telegram WebView). Uses a ref guard
  // so listeners are attached exactly once and never torn down by React
  // re-renders — a teardown gap would let the browser reclaim the touch.
  const touchPreventSetup = useRef(false);

  useEffect(() => {
    const el = cardRef.current;
    if (!el || touchPreventSetup.current) return;
    touchPreventSetup.current = true;

    const onTouchStart = (e: TouchEvent) => {
      e.preventDefault();
    };

    const onTouchMove = (e: TouchEvent) => {
      e.preventDefault();
    };

    // Non-passive + capture phase so we intercept before anything else.
    // Never torn down — intentionally permanent for the element's lifetime.
    el.addEventListener("touchstart", onTouchStart, { passive: false, capture: true });
    el.addEventListener("touchmove", onTouchMove, { passive: false, capture: true });
  });

  const cleanup = useCallback(() => {
    if (cancelAnimRef.current) {
      cancelAnimRef.current();
      cancelAnimRef.current = null;
    }
  }, []);

  // Cleanup is available but not auto-called — caller manages lifecycle
  void cleanup;

  return {
    bind,
    cardRef,
    exitingRef,
    isAnimating: isAnimatingRef.current,
  };
}
