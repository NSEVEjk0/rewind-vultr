const video = document.getElementById("demo-video");
const button = document.getElementById("play-video");
const status = document.getElementById("player-status");
button.addEventListener("click", async () => {
  try {
    if (video.paused || video.ended) {
      await video.play();
    } else {
      video.pause();
    }
  } catch {
    status.textContent = "Your browser could not start playback. Use the video download link below.";
  }
});
video.addEventListener("play", () => { button.textContent = "Pause the video Ⅱ"; });
video.addEventListener("pause", () => { button.textContent = "Play the video ▶"; });
video.addEventListener("ended", () => { button.textContent = "Replay the video ↻"; });
