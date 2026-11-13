import { useRef, useState, type ReactNode } from "react";

interface Props {
  title: string;
  hint: string;
  onFiles: (files: File[]) => void;
  children?: ReactNode;
  compact?: boolean;
}

/** A film strip that takes dropped videos. The button opens the file picker for keyboard and touch users. */
export function UploadStrip({ title, hint, onFiles, children, compact = false }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const take = (list: FileList | null) => {
    const files = Array.from(list ?? []).filter((f) => f.type.startsWith("video/") || /\.(mkv|ts|mts|flv)$/i.test(f.name));
    if (files.length) onFiles(files);
  };
  return (
    <div
      className={["strip", over && "over", compact && "compact"].filter(Boolean).join(" ")}
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
      <div>
        <h2 className="display">{title}</h2>
        <p>{hint}</p>
      </div>
      <div className="controls">
        {children}
        <button type="button" className="btn primary" onClick={() => input.current?.click()}>
          Choose videos
        </button>
        <input
          ref={input}
          type="file"
          accept="video/*,.mkv,.ts,.mts,.flv"
          multiple
          hidden
          onChange={(e) => {
            take(e.target.files);
            e.target.value = "";
          }}
        />
      </div>
    </div>
  );
}
