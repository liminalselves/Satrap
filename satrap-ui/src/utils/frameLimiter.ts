export const MAX_UI_FRAME_RATE = 90;

type FrameCallback = () => void;
type RequestFrame = (callback: FrameRequestCallback) => number;
type CancelFrame = (handle: number) => void;

export interface AnimationFrameLimiter {
  schedule: (callback: FrameCallback) => void;
  flush: () => void;
  cancel: () => void;
}

interface AnimationFrameLimiterOptions {
  maxFps?: number;
  requestFrame?: RequestFrame;
  cancelFrame?: CancelFrame;
}

/** 创建合并重复请求的动画帧限速器 */
export function createAnimationFrameLimiter({
  maxFps = MAX_UI_FRAME_RATE,
  requestFrame = (callback) => requestAnimationFrame(callback),
  cancelFrame = (handle) => cancelAnimationFrame(handle),
}: AnimationFrameLimiterOptions = {}): AnimationFrameLimiter {
  if (!Number.isFinite(maxFps) || maxFps <= 0) {
    throw new RangeError('maxFps 必须是大于 0 的有限数值');
  }

  const minFrameInterval = 1000 / maxFps;
  let frameHandle: number | null = null;
  let lastCommitTime = Number.NEGATIVE_INFINITY;
  let pendingCallback: FrameCallback | null = null;

  const requestNextFrame = () => {
    if (frameHandle !== null) return;
    frameHandle = requestFrame(handleFrame);
  };

  const handleFrame: FrameRequestCallback = (timestamp) => {
    frameHandle = null;
    if (!pendingCallback) return;
    if (timestamp - lastCommitTime < minFrameInterval) {
      requestNextFrame();
      return;
    }

    const callback = pendingCallback;
    pendingCallback = null;
    lastCommitTime = timestamp;
    callback();
  };

  return {
    schedule(callback) {
      pendingCallback = callback;   // 高频请求只保留最新回调, 回调读取已合并的数据
      requestNextFrame();
    },
    flush() {
      if (frameHandle !== null) {
        cancelFrame(frameHandle);
        frameHandle = null;
      }
      const callback = pendingCallback;
      pendingCallback = null;
      callback?.();
    },
    cancel() {
      if (frameHandle !== null) cancelFrame(frameHandle);
      frameHandle = null;
      pendingCallback = null;
      lastCommitTime = Number.NEGATIVE_INFINITY;
    },
  };
}
