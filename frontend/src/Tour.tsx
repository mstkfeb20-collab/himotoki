import { useEffect, useLayoutEffect, useState } from "react";

type Step = { sel?: string; title: string; body: string; before?: () => void };

/** First-run walkthrough: highlights one part of the screen at a time. */
export default function Tour({ steps, onClose, onPractice }: {
  steps: Step[]; onClose: () => void; onPractice: () => void;
}) {
  const [i, setI] = useState(0);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const step = steps[i];

  useLayoutEffect(() => {
    step.before?.();
    const measure = () => {
      const el = step.sel ? document.querySelector(step.sel) : null;
      setRect(el ? el.getBoundingClientRect() : null);
      el?.scrollIntoView?.({ block: "nearest" });
    };
    const t = window.setTimeout(measure, 60);   // let a view switch render first
    window.addEventListener("resize", measure);
    return () => { window.clearTimeout(t); window.removeEventListener("resize", measure); };
  }, [i]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowRight" || e.key === "Enter") setI((x) => Math.min(steps.length - 1, x + 1));
      if (e.key === "ArrowLeft") setI((x) => Math.max(0, x - 1));
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [steps.length, onClose]);

  const pad = 6;
  const W = 360;
  let pop: React.CSSProperties = { left: "50%", top: "50%", transform: "translate(-50%, -50%)", width: W };
  if (rect) {
    // put the bubble next to the highlighted area, never on top of it
    const H = 220, gap = 14, vw = window.innerWidth, vh = window.innerHeight;
    const clampX = (x: number) => Math.min(Math.max(12, x), vw - W - 12);
    const clampY = (y: number) => Math.min(Math.max(12, y), vh - H - 12);
    if (rect.bottom + gap + H < vh) pop = { left: clampX(rect.left), top: rect.bottom + gap, width: W };
    else if (rect.top - gap - H > 0) pop = { left: clampX(rect.left), top: rect.top - gap - H, width: W };
    else if (rect.right + gap + W < vw) pop = { left: rect.right + gap, top: clampY(rect.top), width: W };
    else if (rect.left - gap - W > 0) pop = { left: rect.left - gap - W, top: clampY(rect.top), width: W };
    else pop = { left: rect.right - W - 16, top: rect.bottom - H - 16, width: W };   // inside, bottom-right corner
  }
  const last = i === steps.length - 1;
  return (
    <div className="tour" role="dialog" aria-modal="true" aria-label="使い方の案内">
      {rect ? (
        <div className="tour-hole" style={{ left: rect.left - pad, top: rect.top - pad, width: rect.width + pad * 2, height: rect.height + pad * 2 }} />
      ) : <div className="tour-dim" />}
      <div className="tour-pop pane" style={pop}>
        <div className="row between"><span className="k">使い方 {i + 1} / {steps.length}</span>
          <button className="icon" aria-label="閉じる" onClick={onClose}>×</button></div>
        <h3>{step.title}</h3>
        <p>{step.body}</p>
        <div className="actions">
          {i > 0 && <button className="btn small ghost" onClick={() => setI(i - 1)}>戻る</button>}
          {!last && <button className="btn small light" onClick={() => setI(i + 1)}>次へ</button>}
          {last && <>
            <button className="btn small" onClick={onClose}>閉じる</button>
            <button className="btn small primary" onClick={onPractice}>例文で練習する</button>
          </>}
        </div>
        <div className="tour-dots">{steps.map((_, k) => <i key={k} className={k === i ? "on" : ""} />)}</div>
      </div>
    </div>
  );
}
