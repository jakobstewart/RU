# roop-unleashed

**This fork was created with the intention of providing free use for users. To access the original repository, visit [roop-unleashed on GitHub](https://github.com/C0untFloyd/roop-unleashed).**

Uncensored Faceswaps for images and videos without training and an easy-to-use GUI.

### Features

- Platform-independant Browser GUI
- Selection of multiple input/output faces in one go
- Many different swapping modes, first detected, face selections, by gender
- Batch processing of images/videos
- Masking of face occluders using text prompts or automatically
- Optional Face Upscaler/Restoration using different enhancers
- Preview swapping from different video frames
- Live Fake Cam using your webcam
- Extras Tab for cutting videos etc.
- Settings - storing configuration for next session
- Theme Support

and lots more...

## Disclaimer

This project is for technical and academic use only.
Users of this software are expected to use this software responsibly while abiding by local law. If a face of a real person is being used, users are suggested to get consent from the concerned person and clearly mention that it is a deepfake when posting content online. Developers of this software will not be responsible for actions of end-users.
**Please do not apply it to illegal and unethical scenarios.**

In the event of a violation of the legal and ethical requirements of the user's country or region, this code repository is exempt from liability.

### Installation

Please refer to the [wiki](https://github.com/C0untFloyd/roop-unleashed/wiki).

### Usage

- Windows: run the `windows_run.bat` from the Installer.
- Linux: `python run.py`

Additional commandline arguments are currently unsupported and settings should be done via the UI.

> Note: When you run this program for the first time, it will download some models roughly ~2Gb in size.

### Update for Google Colab Users

<a target="_blank" href="https://colab.research.google.com/drive/1PixFaQs_UD50HrQJheDF7100MD3N1GKd#scrollTo=r_s6t4OQgKms">
  <img src="https://colab.research.google.com/assets/colab-badge.svg" alt="Open In Colab"/>
</a>

Users who do not have Colab Pro could not use the original Colab link without subscribing. Now, with this fork, you can easily use roop on Google Colab.

### Changelog

**22.04.2024** v3.9.0

- Bugfix: Face detection bounding box corrupt values at weird angles
- Rewrote mask previewing to work with every model
- Switching mask engines toggles text interactivity
- Clearing target files, resets face selection dropdown
- Massive rewrite of swapping architecture, needed for xseg implementation
- Added DFL Xseg Support for partial face occlusion
- Face masking only runs when there is a face detected
- Removed unnecessary toggle checkbox for text masking

**22.03.2024** v3.6.5

- Bugfix: Installer pulling latest update on first installation
- Bugfix: Regression issue, blurring/erosion missing from face swap
- Exposed erosion and blur amounts to UI
- Using same values for manual masking too

**20.03.2024** v3.6.3

- Bugfix: Workaround for Gradio Slider Change Bug
- Bugfix: CSS Styling to fix Gradio Image Height Bug
- Made face swapping mask offsets resolution independant
- Show offset mask as overlay
- Changed layout for masking

**18.03.2024** v3.6.0

- Updated to Gradio 4.21.0 - requiring many changes under the hood
- New manual masking (draw the mask yourself)
- Extras Tab, streamlined cutting/joining videos
- Re-added face selection by gender (on-demand loading, default turned off)
- Removed unnecessary activate live-cam option
- Added time info to preview frame and changed frame slider event to allow faster changes

**10.03.2024** v3.5.5

- Bugfix: Installer Path Env
- Bugfix: file attributes
- Video processing checks for presence of ffmpeg and displays warning if not found
- Removed gender + age detection to speed up processing. Option removed from UI
- Replaced restoreformer with restoreformer++
- Live Cam recoded to run separate from virtual cam and without blocking controls
- Swapping with only 1 target face allows selecting from several input faces
