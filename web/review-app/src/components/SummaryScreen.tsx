interface SummaryScreenProps {
  stagedCount: number;
  skippedCount: number;
  hasStagedHunks: boolean;
  onRefresh: () => void;
  onClose: () => void;
  onCommit: () => void;
}

export function SummaryScreen({
  stagedCount,
  skippedCount,
  hasStagedHunks,
  onRefresh,
  onClose,
  onCommit,
}: SummaryScreenProps) {
  const total = stagedCount + skippedCount;

  return (
    <div className="summary-screen">
      <h2>Review Complete</h2>
      <div className="summary-stats">
        <div className="summary-stat">
          <div className="summary-stat-value staged">{stagedCount}</div>
          <div className="summary-stat-label">Staged</div>
        </div>
        <div className="summary-stat">
          <div className="summary-stat-value skipped">{skippedCount}</div>
          <div className="summary-stat-label">Skipped</div>
        </div>
        <div className="summary-stat">
          <div className="summary-stat-value total">{total}</div>
          <div className="summary-stat-label">Total</div>
        </div>
      </div>
      <div className="summary-actions">
        {hasStagedHunks && (
          <button className="summary-btn summary-btn-commit" onClick={onCommit}>
            Commit Staged Changes
          </button>
        )}
        <button className="summary-btn summary-btn-primary" onClick={onClose}>
          Done
        </button>
        <button className="summary-btn summary-btn-secondary" onClick={onRefresh}>
          Refresh &amp; Review Again
        </button>
      </div>
    </div>
  );
}
