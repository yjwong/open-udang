import { useMemo } from "react";
import { useHunks } from "./hooks/useHunks";
import { SwipeDeck } from "./components/SwipeDeck";

function getChatId(): string | null {
  const params = new URLSearchParams(window.location.search);
  return params.get("chat_id");
}

export default function App() {
  const chatId = useMemo(() => getChatId(), []);
  const { hunks, totalHunks, loading, error, refresh } = useHunks(chatId);

  if (loading) {
    return (
      <div className="loading">
        <div className="spinner" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="error-container">
        <p>{error}</p>
        <button onClick={refresh}>Retry</button>
      </div>
    );
  }

  if (hunks.length === 0) {
    return (
      <div className="empty-container">
        <h2>No changes</h2>
        <p>There are no uncommitted changes to review.</p>
      </div>
    );
  }

  return (
    <SwipeDeck
      hunks={hunks}
      totalHunks={totalHunks}
      onRefresh={refresh}
    />
  );
}
