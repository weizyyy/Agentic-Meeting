import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { sendLocalAudio } from "./localAudio.ts";

function fakeSender(track: unknown) {
  const replaced: unknown[] = [];
  const sender = {
    track,
    async replaceTrack(next: unknown) {
      replaced.push(next);
      sender.track = next;
    },
  };
  return { sender, replaced };
}

const track = (id: string) => ({ id }) as unknown as MediaStreamTrack;

describe("sendLocalAudio", () => {
  it("连接发的是旧轨道时换成新的", async () => {
    const { sender, replaced } = fakeSender(track("old"));
    const next = track("new");
    assert.equal(await sendLocalAudio({ getAudioTransceiver: () => ({ sender }) }, next), true);
    assert.deepEqual(replaced, [next]);
  });

  it("发的已经是这条轨道时不动", async () => {
    const same = track("mic");
    const { sender, replaced } = fakeSender(same);
    assert.equal(await sendLocalAudio({ getAudioTransceiver: () => ({ sender }) }, same), false);
    assert.deepEqual(replaced, []);
  });

  it("还没有连接（取收发器出错或取不到）时不动", async () => {
    const throwing = {
      getAudioTransceiver: () => {
        throw new TypeError("Cannot read properties of null (reading 'getTransceivers')");
      },
    };
    assert.equal(await sendLocalAudio(throwing, track("a")), false);
    assert.equal(await sendLocalAudio({ getAudioTransceiver: () => undefined }, track("a")), false);
  });

  it("传输层没有这个方法（SDK 换了实现）时不动", async () => {
    assert.equal(await sendLocalAudio({}, track("a")), false);
    assert.equal(await sendLocalAudio(null, track("a")), false);
  });
});
