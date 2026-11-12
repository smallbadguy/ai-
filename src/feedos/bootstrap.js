/* Mount the adapted scene and follow the replay page's selected event. */
(function () {
  "use strict";
  const host = document.getElementById("scene-host"), job = window.REPLAY_SCENE_JOB;
  Object.assign(window.UI.SRC_LABEL, window.REPLAY_SCENE_LABELS.sourceNames);
  const timeline = window.Council.buildTimeline(job);
  let selected = null, active = 0, frame = 0, frameStart = 0, playbackSpeed = 1, playbackActive = false, lastProgress = 0, lastDrawAt = 0;
  const context = {
    job, tl: timeline, autoOrbit: false, selected,
    select(id) { selected = id; context.selected = id; if (parent.__replay3DFocus) parent.__replay3DFocus(id); },
    openMember(id) { if (parent.__replay3DFocus) parent.__replay3DFocus(id); },
    seek(t) {
      const i = timeline.steps.reduce((n, step, k) => step.start <= t ? k : n, 0);
      if (parent.__replay3DGo) parent.__replay3DGo(i);
    }
  };
  window.FeedViews.scene3d.mount(host, context);
  const view = window.FeedViews.scene3d;
  const stepDuration = (step) => Math.max(400, Math.min(1800, (step.end - step.start) * 1000)) / (playbackActive ? playbackSpeed : 1);
  function draw(now) {
    if (!frameStart) frameStart = now;
    const step = timeline.steps[active];
    if (!step) { frame = 0; return; }
    const frameGap = playbackActive ? (playbackSpeed >= 3 ? 1000 / 30 : playbackSpeed >= 2 ? 1000 / 45 : 0) : 0;
    if (frameGap && lastDrawAt && now - lastDrawAt < frameGap) { frame = requestAnimationFrame(draw); return; }
    lastDrawAt = now;
    const duration = stepDuration(step);
    const k = Math.max(0, Math.min(1, (now - frameStart) / duration));
    lastProgress = k;
    const t = step.start + (step.end - step.start) * k;
    view.update(window.Council.stateAt(timeline, t, null), context);
    frame = k < 1 ? requestAnimationFrame(draw) : 0;
  }
  window.setReplayStep = function (i) {
    active = Math.max(0, Math.min(timeline.steps.length - 1, Number(i) || 0));
    frameStart = 0; lastProgress = 0; lastDrawAt = 0;
    if (frame) cancelAnimationFrame(frame);
    frame = requestAnimationFrame(draw);
  };
  window.setReplayPlayback = function (speed, playing) {
    const wasPlaying = playbackActive;
    playbackSpeed = Math.max(1, Math.min(3, Number(speed) || 1));
    playbackActive = !!playing;
    if (view.setPlaybackQuality) view.setPlaybackQuality(playbackSpeed, playbackActive);
    if (wasPlaying && !playbackActive) {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      const step = timeline.steps[active];
      if (step) {
        lastProgress = 1;
        view.update(window.Council.stateAt(timeline, step.end, null), context);
      }
    } else if (!wasPlaying && playbackActive) {
      const step = timeline.steps[active];
      frameStart = step && lastProgress ? performance.now() - lastProgress * stepDuration(step) : 0;
      lastDrawAt = 0;
      if (frame) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(draw);
    }
  };
  window.setReplayTimezone = function (tz) { window.REPLAY_TZ = tz; window.setReplayStep(active); };
  window.addEventListener("resize", () => view.resize());
  window.setReplayStep(0);
  parent.postMessage({ source: "campaign-replay-3d", type: "ready" }, "*");
})();
