import { useToasts } from "../store/toast";

export function ToastStack() {
  const toasts = useToasts((s) => s.toasts);
  const remove = useToasts((s) => s.remove);
  if (!toasts.length) return null;
  return (
    <div className="toast-stack">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.type}`} onClick={() => remove(t.id)}>
          {t.text}
        </div>
      ))}
    </div>
  );
}
