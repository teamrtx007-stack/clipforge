import { useRef, useState, type ReactNode } from "react";
import { message } from "../api";
import { Icon } from "./Icon";

interface Props {
  onFiles: (files: File[]) => void;
  /** Adds a pasted video link. A rejected promise's message is shown under the box. */
  onLink: (url: string) => Promise<unknown>;
  /** More controls for the bottom row, after "Upload video". */
  children?: ReactNode;
}

const isVideo = (f: File) => f.type.startsWith("video/") || /\.(mkv|ts|mts|flv)$/i.test(f.name);

/** Where new work starts: paste a link, or drop or choose video files. */
export function ClipBox({ onFiles, onLink, children }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [link, setLink] = useState("");
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const take = (list: FileList | null) => {
    const files = Array.from(list ?? []).filter(isVideo);
    if (files.length) onFiles(files);
  };

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!link.trim()) return input.current?.click(); // "Make clips" with no link picks files instead
    setAdding(true);
    setError(null);
    try {
      await onLink(link.trim());
      setLink("");
    } catch (err) {
      setError(message(err));
    } finally {
      setAdding(false);
    }
  }

  return (
    <form
      className={over ? "clipbox over" : "clipbox"}
      onSubmit={submit}
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        take(e.dataTransfer.files);
      }}
    >
      <label className="clipbox-link">
        <Icon name="link" />
        <input type="url" value={link} onChange={(e) => setLink(e.target.value)} aria-label="Video link"
          placeholder="Paste a YouTube, Twitch or Kick video link" />
      </label>
      <div className="clipbox-row">
        <button type="button" className="pill" onClick={() => input.current?.click()}>
          <Icon name="upload" size={14} />Upload video
        </button>
        {children}
        <span className="drop-hint">or drop a video file here</span>
        <button className="btn primary" type="submit" disabled={adding}>
          <Icon name="scissors" size={15} />{adding ? "Adding…" : "Make clips"}
        </button>
      </div>
      {error && <p className="error-text clipbox-error">{error}</p>}
      <input ref={input} type="file" accept="video/*,.mkv,.ts,.mts,.flv" multiple hidden
        onChange={(e) => {
          take(e.target.files);
          e.target.value = "";
        }} />
    </form>
  );
}
