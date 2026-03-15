import { useState, useCallback, useEffect, useRef } from "react";
import type { Hunk } from "../lib/types";
import { stageHunk, unstageHunk, StaleHunkError } from "../lib/api";
import { HunkCard } from "./HunkCard";
import { FilePicker } from "./FilePicker";
import { ProgressBar } from "./ProgressBar";
import { SummaryScreen } from "./SummaryScreen";
import { useSwipe, type SwipeDirection } from "../hooks/useSwipe";

interface SwipeDeckProps {
  hunks: Hunk[];
  totalHunks: number;
  chatId: string;
  dir: string;
  onRefresh: () => void;
  onNeedMore?: () => void;
}

interface Decision {
  hunkId: string;
  action: "staged" | "skipped";
  wasAlreadyStaged: boolean;
}

export function SwipeDeck({
  hunks,
  totalHunks,
  chatId,
  dir,
  onRefresh,
  onNeedMore,
}: SwipeDeckProps) {
  const [currentIndex, setCurrentIndex] = useState(0);
  const [history, setHistory] = useState<Decision[]>([]);
  const [stagedCount, setStagedCount] = useState(0);
  const [skippedCount, setSkippedCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);
  const swipeEnabled = !isProcessing;

  // Track swipe direction for overlay — use refs + direct DOM updates
  // to avoid React re-renders during drag (which would cause @use-gesture
  // to tear down and re-bind, killing the active gesture).
  const overlayRightRef = useRef<HTMLDivElement | null>(null);
  const overlayLeftRef = useRef<HTMLDivElement | null>(null);
  const overlayDownRef = useRef<HTMLDivElement | null>(null);
  const dragDirectionRef = useRef<"left" | "right" | "down" | null>(null);

  const currentHunk = hunks[currentIndex] ?? null;
  const nextHunk = hunks[currentIndex + 1] ?? null;
  const isComplete = currentIndex >= hunks.length;

  // Request more hunks when nearing the end
  useEffect(() => {
    if (onNeedMore && currentIndex >= hunks.length - 3 && hunks.length < totalHunks) {
      onNeedMore();
    }
  }, [currentIndex, hunks.length, totalHunks, onNeedMore]);

  const handleSwipe = useCallback(
    async (direction: SwipeDirection) => {
      if (!direction || !currentHunk) return;

      setError(null);

      if (direction === "down") {
        // Undo last decision
        const lastDecision = history[history.length - 1];
        if (!lastDecision) return;

        setIsProcessing(true);
        try {
          // If we staged the hunk, unstage it
          if (lastDecision.action === "staged" && !lastDecision.wasAlreadyStaged) {
            await unstageHunk(lastDecision.hunkId, chatId, dir);
          }

          setHistory((h) => h.slice(0, -1));
          setCurrentIndex((i) => Math.max(0, i - 1));

          if (lastDecision.action === "staged") {
            setStagedCount((c) => Math.max(0, c - 1));
          } else {
            setSkippedCount((c) => Math.max(0, c - 1));
          }
        } catch (err) {
          if (err instanceof StaleHunkError) {
            setError("Changes detected — please refresh");
          } else {
            setError(err instanceof Error ? err.message : "Failed to undo");
          }
        } finally {
          setIsProcessing(false);
        }
        return;
      }

      if (direction === "right") {
        // Stage the hunk
        setIsProcessing(true);
        try {
          if (!currentHunk.staged) {
            await stageHunk(currentHunk.id, chatId, dir);
          }
          setHistory((h) => [
            ...h,
            {
              hunkId: currentHunk.id,
              action: "staged",
              wasAlreadyStaged: currentHunk.staged,
            },
          ]);
          setStagedCount((c) => c + 1);
          setCurrentIndex((i) => i + 1);
        } catch (err) {
          if (err instanceof StaleHunkError) {
            setError("Changes detected — please refresh");
          } else {
            setError(err instanceof Error ? err.message : "Failed to stage hunk");
          }
        } finally {
          setIsProcessing(false);
        }
        return;
      }

      if (direction === "left") {
        // Skip (or unstage if already staged)
        setIsProcessing(true);
        try {
          if (currentHunk.staged) {
            await unstageHunk(currentHunk.id, chatId, dir);
          }
          setHistory((h) => [
            ...h,
            {
              hunkId: currentHunk.id,
              action: "skipped",
              wasAlreadyStaged: currentHunk.staged,
            },
          ]);
          setSkippedCount((c) => c + 1);
          setCurrentIndex((i) => i + 1);
        } catch (err) {
          if (err instanceof StaleHunkError) {
            setError("Changes detected — please refresh");
          } else {
            setError(err instanceof Error ? err.message : "Failed to skip hunk");
          }
        } finally {
          setIsProcessing(false);
        }
      }
    },
    [currentHunk, history, chatId, dir],
  );

  // When swipe threshold is reached, clear the drag overlay immediately
  const handleSwipeThreshold = useCallback((_direction: SwipeDirection) => {
    dragDirectionRef.current = null;
    overlayRightRef.current?.classList.remove("active");
    overlayLeftRef.current?.classList.remove("active");
    overlayDownRef.current?.classList.remove("active");
  }, []);

  const { bind, cardRef, exitingRef } = useSwipe({
    onSwipe: handleSwipe,
    onSwipeThreshold: handleSwipeThreshold,
    enabled: swipeEnabled && !isComplete,
  });

  // Track drag for overlay — listen to style changes on the card.
  // Uses direct DOM manipulation instead of React state to avoid
  // re-renders that would kill the @use-gesture drag session.
  useEffect(() => {
    const el = cardRef.current;
    if (!el) return;

    const observer = new MutationObserver(() => {
      const transform = el.style.transform;
      const match = transform.match(
        /translate3d\(([^,]+)px,\s*([^,]+)px/,
      );

      let newDir: "left" | "right" | "down" | null = null;
      if (match) {
        const x = parseFloat(match[1]!);
        const y = parseFloat(match[2]!);
        if (Math.abs(x) > 20) {
          newDir = x > 0 ? "right" : "left";
        } else if (y > 20) {
          newDir = "down";
        }
      }

      if (newDir !== dragDirectionRef.current) {
        dragDirectionRef.current = newDir;
        // Update overlay classes directly
        overlayRightRef.current?.classList.toggle("active", newDir === "right");
        overlayLeftRef.current?.classList.toggle("active", newDir === "left");
        overlayDownRef.current?.classList.toggle("active", newDir === "down");
      }
    });

    observer.observe(el, { attributes: true, attributeFilter: ["style"] });
    return () => observer.disconnect();
  }, [cardRef, currentIndex]);

  const pendingSkipRef = useRef(false);

  const skipToFirstUnstaged = useCallback(
    (fromIndex: number) => {
      for (let i = fromIndex; i < hunks.length; i++) {
        if (!hunks[i]!.staged) {
          if (i !== fromIndex) {
            const newDecisions: Decision[] = [];
            for (let j = fromIndex; j < i; j++) {
              newDecisions.push({
                hunkId: hunks[j]!.id,
                action: "skipped",
                wasAlreadyStaged: hunks[j]!.staged,
              });
              setSkippedCount((c) => c + 1);
            }
            setHistory((h) => [...h, ...newDecisions]);
            setCurrentIndex(i);
          }
          return true;
        }
      }
      return false;
    },
    [hunks],
  );

  const handleSkipToUnstaged = useCallback(() => {
    if (skipToFirstUnstaged(currentIndex)) {
      pendingSkipRef.current = false;
      return;
    }
    if (hunks.length < totalHunks && onNeedMore) {
      pendingSkipRef.current = true;
      onNeedMore();
    }
  }, [currentIndex, hunks.length, totalHunks, onNeedMore, skipToFirstUnstaged]);

  useEffect(() => {
    if (!pendingSkipRef.current) return;

    if (skipToFirstUnstaged(currentIndex)) {
      pendingSkipRef.current = false;
    } else if (hunks.length < totalHunks && onNeedMore) {
      onNeedMore();
    } else {
      pendingSkipRef.current = false;
    }
  }, [hunks.length, currentIndex, totalHunks, onNeedMore, skipToFirstUnstaged]);

  const handleJumpToFile = useCallback(
    (hunkIndex: number) => {
      if (hunkIndex === currentIndex) return;
      setError(null);
      setCurrentIndex(hunkIndex);
      // Don't record decisions for jumps — the user is navigating, not reviewing
    },
    [currentIndex],
  );

  const hasUnstagedAhead = hunks.some(
    (h, i) => i >= currentIndex && !h.staged,
  );
  const currentIsUnstaged = currentHunk ? !currentHunk.staged : false;
  const showSkipButton =
    hasUnstagedAhead && !currentIsUnstaged && !isComplete;

  const handleRefresh = useCallback(() => {
    setCurrentIndex(0);
    setHistory([]);
    setStagedCount(0);
    setSkippedCount(0);
    setError(null);
    onRefresh();
  }, [onRefresh]);

  const handleClose = useCallback(() => {
    try {
      window.Telegram?.WebApp?.close();
    } catch {
      // Not in Telegram context
    }
  }, []);

  if (isComplete) {
    return (
      <SummaryScreen
        stagedCount={stagedCount}
        skippedCount={skippedCount}
        onRefresh={handleRefresh}
        onClose={handleClose}
      />
    );
  }

  return (
    <div className="swipe-deck">
      <div className="swipe-deck-toolbar">
        <ProgressBar current={currentIndex} total={totalHunks} />
        <FilePicker
          hunks={hunks}
          currentIndex={currentIndex}
          onJumpToFile={handleJumpToFile}
          disabled={isProcessing}
        />
        {showSkipButton && (
          <button
            className="skip-to-unstaged-btn"
            onClick={handleSkipToUnstaged}
            disabled={isProcessing}
            title="Skip to first unstaged hunk"
          >
            Skip to unstaged ⏭
          </button>
        )}
      </div>

      {error && (
        <div className="swipe-error">
          <span>{error}</span>
          <button onClick={handleRefresh}>Refresh</button>
        </div>
      )}

      <div className="card-container">
        {/* Next card (peeking underneath) */}
        {nextHunk && (
          <div className="swipe-card swipe-card-next">
            <HunkCard hunk={nextHunk} />
          </div>
        )}

        {/* Current card (interactive) */}
        {currentHunk && (
          <div
            {...bind()}
            ref={cardRef}
            className="swipe-card swipe-card-current"
            style={{ touchAction: "none" }}
          >
            {/* Swipe direction overlays — classes toggled via refs, not state */}
            <div
              ref={overlayRightRef}
              className="swipe-overlay swipe-overlay-right"
            >
              {currentHunk.staged ? "Keep" : "Stage"}
            </div>
            <div
              ref={overlayLeftRef}
              className={`swipe-overlay swipe-overlay-left${currentHunk.staged ? " swipe-overlay-unstage" : ""}`}
            >
              {currentHunk.staged ? "Unstage" : "Skip"}
            </div>
            <div
              ref={overlayDownRef}
              className="swipe-overlay swipe-overlay-down"
            >
              Undo
            </div>
            <HunkCard hunk={currentHunk} />
          </div>
        )}

        {/* Exiting card layer — used for the fly-off animation.
            Content is cloned from the current card when a swipe is confirmed,
            so the real card can immediately show the next hunk underneath. */}
        <div
          ref={exitingRef}
          className="swipe-card swipe-card-exiting"
          style={{ display: "none" }}
        />
      </div>
    </div>
  );
}
