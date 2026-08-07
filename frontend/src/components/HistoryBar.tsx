interface Props {
  count: number;
  index: number;
  canClear: boolean;
  onPrev: () => void;
  onNext: () => void;
  onClear: () => void;
}

export function HistoryBar({ count, index, canClear, onPrev, onNext, onClear }: Props) {
  if (count < 2) return null; // hidden until 2+ entries
  return (
    <div className="history-bar">
      <button
        type="button"
        className="mini-button"
        onClick={onClear}
        disabled={!canClear}
      >
        Clear
      </button>
      <span className="history-spacer" />
      <button
        type="button"
        className="mini-button"
        aria-label="Previous answer"
        onClick={onPrev}
        disabled={index === 0}
      >
        ←
      </button>
      <span className="history-label">
        {index + 1}/{count}
      </span>
      <button
        type="button"
        className="mini-button"
        aria-label="Next answer"
        onClick={onNext}
        disabled={index >= count - 1}
      >
        →
      </button>
    </div>
  );
}
