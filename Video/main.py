from utils import Video
import utils

if __name__ == "__main__":
    path = "Video/videos/poisson1.mp4"
    video = Video(path)
    # video.plot_images()
    utils.build_video(video.video_resized)
