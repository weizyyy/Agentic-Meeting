import { meterFraction, meterZone, toDbfs } from "../micLevel.ts";
import { useMicLevel } from "../useMicLevel.ts";

const ZONE_TEXT = {
  silent: "几乎没有声音",
  low: "偏轻，识别可能不稳",
  ok: "正常",
} as const;

interface Props {
  track: MediaStreamTrack | null;
}

/** 麦克风电平条：浏览器采集后、服务端增益之前的电平，一眼看出麦克风有没有收到声音、有多轻。 */
export function MicMeter({ track }: Props) {
  const rms = useMicLevel(track);
  if (!track) return null;
  const zone = meterZone(rms);
  const db = toDbfs(rms);
  const label = Number.isFinite(db) ? `${db.toFixed(0)} dBFS` : "静音";
  return (
    <div
      className={`mic-meter mic-meter-${zone}`}
      role="meter"
      aria-label="麦克风电平"
      aria-valuemin={0}
      aria-valuemax={1}
      aria-valuenow={Number(meterFraction(rms).toFixed(2))}
      title={`麦克风电平（服务端增益之前）：${label}，${ZONE_TEXT[zone]}`}
    >
      <span className="mic-meter-icon" aria-hidden="true">
        🎙
      </span>
      <span className="mic-meter-track">
        <span className="mic-meter-fill" style={{ width: `${meterFraction(rms) * 100}%` }} />
      </span>
    </div>
  );
}
