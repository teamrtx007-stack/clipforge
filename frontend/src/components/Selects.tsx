import { api, type ClipLength, type ProjectSettings } from "../api";
import { LENGTH_LABELS } from "../format";
import { usePoll } from "../hooks";
import { Icon } from "./Icon";

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

/** Clip length, template and clip count, folded into an "Options" pill. */
export function ClipOptions({ value, onChange }: { value: ProjectSettings; onChange: (v: ProjectSettings) => void }) {
  return (
    <details className="pill-menu">
      <summary className="pill"><Icon name="sliders" size={14} />Options<Icon name="chevron" size={13} /></summary>
      <div className="menu">
        <label className="field">Clip length
          <LengthSelect value={value.length} onChange={(length) => onChange({ ...value, length })} />
        </label>
        <label className="field">Template
          <TemplateSelect value={value.template} onChange={(template) => onChange({ ...value, template })} />
        </label>
        <label className="field">Clips per video, at most
          <input type="number" min={1} max={30} value={value.max_clips}
            onChange={(e) => e.target.valueAsNumber >= 1 &&
              onChange({ ...value, max_clips: Math.min(30, Math.round(e.target.valueAsNumber)) })} />
        </label>
      </div>
    </details>
  );
}
