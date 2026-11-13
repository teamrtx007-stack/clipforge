import { api, type ClipLength } from "../api";
import { LENGTH_LABELS } from "../format";
import { usePoll } from "../hooks";

export function LengthSelect({ value, onChange }: { value: ClipLength; onChange: (v: ClipLength) => void }) {
  return (
    <select aria-label="Clip length" value={value} onChange={(e) => onChange(e.target.value as ClipLength)}>
      {Object.entries(LENGTH_LABELS).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
    </select>
  );
}

export function TemplateSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const { data: templates } = usePoll(api.templates, 600000);
  return (
    <select aria-label="Template" value={value} onChange={(e) => onChange(e.target.value)}>
      {(templates ?? [{ id: value, name: value }]).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
    </select>
  );
}
