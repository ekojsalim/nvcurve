interface Props {
  message: string;
  detail?: string;
  confirmLabel?: string;
  isDestructive?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  message,
  detail,
  confirmLabel = 'Confirm',
  isDestructive = false,
  onConfirm,
  onCancel,
}: Props) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: 'rgba(0,0,0,0.7)' }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel(); }}
    >
      <div className="bg-zinc-900 border border-zinc-700 rounded-xl shadow-2xl p-6 w-96 max-w-[90vw]">
        <h2 className="text-zinc-100 font-semibold text-sm mb-1">{message}</h2>
        {detail && <p className="text-zinc-400 text-xs mb-4">{detail}</p>}
        {!detail && <div className="mb-4" />}
        <div className="flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 rounded bg-zinc-800 hover:bg-zinc-700 text-zinc-300 text-xs transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className={[
              'px-3 py-1.5 rounded text-xs font-semibold transition-colors',
              isDestructive
                ? 'bg-red-600 hover:bg-red-500 text-white'
                : 'bg-emerald-600 hover:bg-emerald-500 text-white',
            ].join(' ')}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
