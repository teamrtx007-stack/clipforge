import { api } from "../api";
import { usePoll } from "../hooks";

/** Says when ClipForge deletes videos and clips (KEEP_HOURS), so nobody loses a clip they meant to keep. */
export function KeepNote() {
  const { data } = usePoll(api.system, 60000);
  const hours = data?.keep_hours;
  if (!hours) return null;
  return (
    <p className="keep-note">
      ClipForge deletes videos and their clips {hours} hour{hours === 1 ? "" : "s"} after you add them. Download the
      clips you want to keep.
    </p>
  );
}
