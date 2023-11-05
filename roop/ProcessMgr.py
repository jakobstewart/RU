import os
import cv2 
import numpy as np
import psutil

from roop.ProcessOptions import ProcessOptions

from roop.face_util import get_first_face, get_all_faces, rotate_image_180, rotate_image_90, rotate_anticlockwise, rotate_clockwise
from roop.utilities import compute_cosine_distance, get_device, str_to_class

from typing import Any, List, Callable
from roop.typing import Frame
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Thread, Lock
from queue import Queue
from tqdm import tqdm
from roop.ffmpeg_writer import FFMPEG_VideoWriter
import roop.globals


def create_queue(temp_frame_paths: List[str]) -> Queue[str]:
    queue: Queue[str] = Queue()
    for frame_path in temp_frame_paths:
        queue.put(frame_path)
    return queue


def pick_queue(queue: Queue[str], queue_per_future: int) -> List[str]:
    queues = []
    for _ in range(queue_per_future):
        if not queue.empty():
            queues.append(queue.get())
    return queues


class ProcessMgr():
    input_face_datas = []
    target_face_datas = []

    processors = []
    options : ProcessOptions = None
    
    num_threads = 1
    current_index = 0
    processing_threads = 1
    buffer_wait_time = 0.1

    lock = Lock()

    frames_queue = None
    processed_queue = None

    videowriter= None

    progress_gradio = None
    total_frames = 0

    


    plugins =  { 
    'faceswap'      : 'FaceSwapInsightFace',
    'mask_clip2seg' : 'Mask_Clip2Seg',
    'codeformer'    : 'Enhance_CodeFormer',
    'gfpgan'        : 'Enhance_GFPGAN',
    'dmdnet'        : 'Enhance_DMDNet',
    'gpen'          : 'Enhance_GPEN',
    }

    def __init__(self, progress):
        if progress is not None:
            self.progress_gradio = progress


    def initialize(self, input_faces, target_faces, options):
        self.input_face_datas = input_faces
        self.target_face_datas = target_faces
        self.options = options

        processornames = options.processors.split(",")
        devicename = get_device()
        if len(self.processors) < 1:
            for pn in processornames:
                classname = self.plugins[pn]
                module = 'roop.processors.' + classname
                p = str_to_class(module, classname)
                p.Initialize(devicename)
                self.processors.append(p)
        else:
            for i in range(len(self.processors) -1, -1, -1):
                if not self.processors[i].processorname in processornames:
                    self.processors[i].Release()
                    del self.processors[i]

            for i,pn in enumerate(processornames):
                if i >= len(self.processors) or self.processors[i].processorname != pn:
                    p = None
                    classname = self.plugins[pn]
                    module = 'roop.processors.' + classname
                    p = str_to_class(module, classname)
                    p.Initialize(devicename)
                    if p is not None:
                        self.processors.insert(i, p)



    def run_batch(self, source_files, target_files, threads:int = 1):
        progress_bar_format = '{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]'
        self.total_frames = len(source_files)
        self.num_threads = threads
        with tqdm(total=self.total_frames, desc='Processing', unit='frame', dynamic_ncols=True, bar_format=progress_bar_format) as progress:
            with ThreadPoolExecutor(max_workers=threads) as executor:
                futures = []
                queue = create_queue(source_files)
                queue_per_future = max(len(source_files) // threads, 1)
                while not queue.empty():
                    future = executor.submit(self.process_frames, source_files, target_files, pick_queue(queue, queue_per_future), lambda: self.update_progress(progress))
                    futures.append(future)
                for future in as_completed(futures):
                    future.result()


    def process_frames(self, source_files: List[str], target_files: List[str], current_files, update: Callable[[], None]) -> None:
        for f in current_files:
            if not roop.globals.processing:
                return
            
            temp_frame = cv2.imread(f)
            if temp_frame is not None:
                resimg = self.process_frame(temp_frame)
                if resimg is not None:
                    i = source_files.index(f)
                    cv2.imwrite(target_files[i], resimg)
            if update:
                update()



    def read_frames_thread(self, cap, frame_start, frame_end, num_threads):
        num_frame = 0
        total_num = frame_end - frame_start
        if frame_start > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES,frame_start)

        while True and roop.globals.processing:
            ret, frame = cap.read()
            if not ret:
                break
                
            self.frames_queue[num_frame % num_threads].put(frame, block=True)
            num_frame += 1
            if num_frame == total_num:
                break

        for i in range(num_threads):
            self.frames_queue[i].put(None)



    def process_videoframes(self, threadindex, progress) -> None:
        while True:
            frame = self.frames_queue[threadindex].get()
            if frame is None:
                self.processing_threads -= 1
                self.processed_queue[threadindex].put((False, None))
                return
            else:
                resimg = self.process_frame(frame)
                self.processed_queue[threadindex].put((True, resimg))
                del frame
                progress()


    def write_frames_thread(self):
        nextindex = 0
        num_producers = self.num_threads
        
        while True:
            process, frame = self.processed_queue[nextindex % self.num_threads].get()
            nextindex += 1
            if frame is not None:
                self.videowriter.write_frame(frame)
                del frame
            elif process == False:
                num_producers -= 1
                if num_producers < 1:
                    return
            


    def run_batch_inmem(self, source_video, target_video, frame_start, frame_end, fps, threads:int = 1, skip_audio=False):
        cap = cv2.VideoCapture(source_video)
        # frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frame_count = (frame_end - frame_start) + 1
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        self.total_frames = frame_count
        self.num_threads = threads

        self.processing_threads = self.num_threads
        self.frames_queue = []
        self.processed_queue = []
        for _ in range(threads):
            self.frames_queue.append(Queue(1))
            self.processed_queue.append(Queue(1))

        self.videowriter =  FFMPEG_VideoWriter(target_video, (width, height), fps, codec=roop.globals.video_encoder, crf=roop.globals.video_quality, audiofile=None)

        readthread = Thread(target=self.read_frames_thread, args=(cap, frame_start, frame_end, threads))
        readthread.start()

        writethread = Thread(target=self.write_frames_thread)
        writethread.start()

        progress_bar_format = '{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}{postfix}]'
        with tqdm(total=self.total_frames, desc='Processing', unit='frames', dynamic_ncols=True, bar_format=progress_bar_format) as progress:
            with ThreadPoolExecutor(thread_name_prefix='swap_proc', max_workers=self.num_threads) as executor:
                futures = []
                
                for threadindex in range(threads):
                    future = executor.submit(self.process_videoframes, threadindex, lambda: self.update_progress(progress))
                    futures.append(future)
                
                for future in as_completed(futures):
                    future.result()
        # wait for the task to complete
        readthread.join()
        writethread.join()
        cap.release()
        self.videowriter.close()
        self.frames_queue.clear()
        self.processed_queue.clear()




    def update_progress(self, progress: Any = None) -> None:
        process = psutil.Process(os.getpid())
        memory_usage = process.memory_info().rss / 1024 / 1024 / 1024
        msg = 'memory_usage: ' + '{:.2f}'.format(memory_usage).zfill(5) + f' GB execution_threads {self.num_threads}'
        progress.set_postfix({
            'memory_usage': '{:.2f}'.format(memory_usage).zfill(5) + 'GB',
            'execution_threads': self.num_threads
        })
        progress.update(1)
        self.progress_gradio((progress.n, self.total_frames), desc='Processing', total=self.total_frames, unit='frames')


    def on_no_face_action(self, frame:Frame):
        if roop.globals.no_face_action == 0:
            return None, frame
        elif roop.globals.no_face_action == 2:
            return None, None

        
        faces = get_all_faces(frame)
        if faces is not None:
            return faces, frame
        return None, frame
      

    def process_frame(self, frame:Frame):
        use_original_frame = 0
        retry_rotated_180 = 1
        skip_frame = 2

        if len(self.input_face_datas) < 1:
            return frame
        temp_frame = frame.copy()
        num_swapped, temp_frame = self.swap_faces(frame, temp_frame)
        if num_swapped > 0:
            return temp_frame
        if roop.globals.no_face_action == use_original_frame:
            return frame
        if roop.globals.no_face_action == skip_frame:
            #This only works with in-mem processing, as it simply skips the frame.
            #For 'extract frames' it simply leaves the unprocessed frame unprocessed and it gets used in the final output by ffmpeg.
            #If we could delete that frame here, that'd work but that might fuck up ffmpeg, and I don't think we have the info on what frame it actually is?????
            #alternatively, it could mark all the necessary frames for deletion, delete them at the end, then rename the remaining frames that might work?
            #alternatively to that we just get this auto-rotation business working for in-mem processing, which would be ideal!
            #fucking spaghetti code this is man... but even in all its noodleyness, boy is it good.
            return None
        else:
            copyframe = frame.copy()
            copyframe = rotate_image_180(copyframe)
            temp_frame = copyframe.copy()
            num_swapped, temp_frame = self.swap_faces(copyframe, temp_frame)
            if num_swapped == 0:
                return frame
            temp_frame = rotate_image_180(temp_frame)
            return temp_frame


    def swap_faces(self, frame, temp_frame):
        num_faces_found = 0

        if self.options.swap_mode == "first":
            face = get_first_face(frame)

            if face is None:
                return num_faces_found, frame
            
            num_faces_found += 1
            temp_frame = self.process_face(self.options.selected_index, face, temp_frame)
        else:
            faces = get_all_faces(frame)
            if faces is None:
                return num_faces_found, frame

            if self.options.swap_mode == "single_face_frames_only":
                if len(faces) == 1:
                    num_faces_found += 1
                    target_face = faces[0]
                    
                    temp_frame = self.process_face(self.options.selected_index, target_face, temp_frame)
                    
                    input_face = self.input_face_datas[self.options.selected_index].faces[0]
                    rotation_action = self.rotation_action(target_face, frame)
                    swapped_face = None
                    optimal_frame = temp_frame.copy()
                    
                    # before we try and get the swapped face again, we need to make sure we're getting it from the most optimal version of the frame
                    # otherwise it sometimes doesn't detect it, so if it needs to be rotated, then do that first.
                    if rotation_action == "rotate_clockwise":
                        optimal_frame = rotate_clockwise(optimal_frame)
                    elif rotation_action == "rotate_anticlockwise":
                        optimal_frame = rotate_anticlockwise(optimal_frame)
                    
                    swapped_face = get_first_face(optimal_frame)

                    if swapped_face is None:
                        num_faces_found = 0
                        return num_faces_found, frame
                    else:
                        # check if the face matches closely the face we intended to swap it too
                        # if it doesn't, it's probably insightface failing and returning some garbled mess, so skip it
                        cosine_distance = compute_cosine_distance(swapped_face.embedding, input_face.embedding)
                        if cosine_distance >= self.options.face_distance_threshold:
                            num_faces_found = 0
                            return num_faces_found, frame
                else:
                    return num_faces_found, frame
            
            if self.options.swap_mode == "all":
                for face in faces:
                    num_faces_found += 1
                    temp_frame = self.process_face(self.options.selected_index, face, temp_frame)
                    del face
            
            elif self.options.swap_mode == "selected":
                for i,tf in enumerate(self.target_face_datas):
                    for face in faces:
                        if compute_cosine_distance(tf.embedding, face.embedding) <= self.options.face_distance_threshold:
                            if i < len(self.input_face_datas):
                                temp_frame = self.process_face(i, face, temp_frame)
                                num_faces_found += 1
                            break
                        del face
            elif self.options.swap_mode == "all_female" or self.options.swap_mode == "all_male":
                gender = 'F' if self.options.swap_mode == "all_female" else 'M'
                for face in faces:
                    if face.sex == gender:
                        num_faces_found += 1
                        temp_frame = self.process_face(self.options.selected_index, face, temp_frame)
                    del face

        if num_faces_found == 0:
            return num_faces_found, frame

        maskprocessor = next((x for x in self.processors if x.processorname == 'clip2seg'), None)
        if maskprocessor is not None:
            temp_frame = self.process_mask(maskprocessor, frame, temp_frame)
        return num_faces_found, temp_frame


    def rotation_action(self, original_face, frame:Frame):
        (height, width) = frame.shape[:2]

        bounding_box_width = original_face.bbox[2] - original_face.bbox[0]
        bounding_box_height = original_face.bbox[3] - original_face.bbox[1]
        horizontal_face = bounding_box_width > bounding_box_height

        center_x = width // 2.0
        start_x = original_face.bbox[0]
        end_x = original_face.bbox[2]
        bbox_center_x = start_x + (bounding_box_width // 2.0)

        # need to leverage the array of landmarks as decribed here:
        # https://github.com/deepinsight/insightface/tree/master/alignment/coordinate_reg
        # basically, we should be able to check for the relative position of eyes and nose
        # then use that to determine which way the face is actually facing when in a horizontal position
        # and use that to determine the correct rotation_action

        if horizontal_face:
            if bbox_center_x >= center_x:
                #this is someone lying down with their face in the right hand side of the frame
                return "rotate_anticlockwise"
            if bbox_center_x < center_x:
                #this is someone lying down with their face in the left hand side of the frame
                return "rotate_clockwise"

        return "noop"


    def auto_rotate_frame(self, original_face, frame:Frame):
        target_face = original_face
        original_frame = frame

        rotation_action = self.rotation_action(original_face, frame)

        if rotation_action == "rotate_anticlockwise":
            print("face is horizontal, rotating frame anti-clockwise and getting face bounding box from rotated frame")
            rotated_bbox = self.rotate_bbox_anticlockwise(original_face.bbox, frame)
            frame = rotate_anticlockwise(frame)
            target_face = self.get_rotated_target_face(rotated_bbox, frame)
        elif rotation_action == "rotate_clockwise":
            print("face is horizontal, rotating frame clockwise and getting face bounding box from rotated frame")
            rotated_bbox = self.rotate_bbox_clockwise(original_face.bbox, frame)
            frame = rotate_clockwise(frame)
            target_face = self.get_rotated_target_face(rotated_bbox, frame)
        else:
            print("face is vertical, leaving frame untouched")

        if target_face is None:
            #no face was detected in the rotated frame, so use the original frame and face
            target_face = original_face
            frame = original_frame
            rotation_action = "noop"

        return target_face, frame, rotation_action


    def auto_unrotate_frame(self, frame:Frame, rotation_action):
        if rotation_action == "rotate_anticlockwise":
            print("frame was rotated anti-clockwise, rotating processed frame clockwise")
            return rotate_clockwise(frame)
        elif rotation_action == "rotate_clockwise":
            print("frame was rotated clockwise, rotating processed frame anti-clockwise")
            return rotate_anticlockwise(frame)
        
        print("face was vertical, leaving processed frame untouched")
        return frame


    def get_rotated_target_face(self, rotated_bbox, rotated_frame:Frame):
        rotated_faces = get_all_faces(rotated_frame)

        if not rotated_faces:
            return None

        rotated_target_face = rotated_faces[0]
        best_iou = 0

        for rotated_face in rotated_faces:
            iou = self.intersection_over_union(rotated_bbox, rotated_face.bbox)
            if iou > best_iou:
                rotated_target_face = rotated_face
                best_iou = iou
            
        print(f"closest matching face - iou: {best_iou}, bbox: {rotated_target_face.bbox}, rotated bbox: {rotated_bbox}")
        return rotated_target_face


    def rotate_bbox_clockwise(self, bbox, frame:Frame):
        (height, width) = frame.shape[:2]

        start_x = bbox[0]
        start_y = bbox[1]
        end_x = bbox[2]
        end_y = bbox[3]

        #bottom left corner becomes top left corner
        #top right corner becomes bottom right corner

        rotated_start_x = height - end_y
        rotated_start_y = start_x
        rotated_end_x = height - start_y
        rotated_end_y = end_x

        return [rotated_start_x, rotated_start_y, rotated_end_x, rotated_end_y]


    def rotate_bbox_anticlockwise(self, bbox, frame:Frame):
        
        (height, width) = frame.shape[:2]

        start_x = bbox[0]
        start_y = bbox[1]
        end_x = bbox[2]
        end_y = bbox[3]

        # So the algorithm is 
        # - top right corner translates to top left corner which gives start_x, start_y and is calculated as follows: (start_y, width - end_x)
        # - bottom left corner translates to bottom right corner giving end_x, end_y and is calculated as follows:  (end_y, width - start_x)

        rotated_start_x = start_y
        rotated_start_y = width - end_x
        rotated_end_x = end_y
        rotated_end_y = width - start_x

        return [rotated_start_x, rotated_start_y, rotated_end_x, rotated_end_y]


    def intersection_over_union(self,boxA, boxB):
        # https://pyimagesearch.com/2016/11/07/intersection-over-union-iou-for-object-detection/
        # determine the (x, y)-coordinates of the intersection rectangle
        xA = max(boxA[0], boxB[0])
        yA = max(boxA[1], boxB[1])
        xB = min(boxA[2], boxB[2])
        yB = min(boxA[3], boxB[3])
        # compute the area of intersection rectangle
        interArea = max(0, xB - xA + 1) * max(0, yB - yA + 1)
        # compute the area of both the prediction and ground-truth
        # rectangles
        boxAArea = (boxA[2] - boxA[0] + 1) * (boxA[3] - boxA[1] + 1)
        boxBArea = (boxB[2] - boxB[0] + 1) * (boxB[3] - boxB[1] + 1)
        # compute the intersection over union by taking the intersection
        # area and dividing it by the sum of prediction + ground-truth
        # areas - the interesection area
        iou = interArea / float(boxAArea + boxBArea - interArea)
        # return the intersection over union value
        return iou


    def process_face(self,face_index, target_face, frame:Frame):
        target_face, frame, rotation_action = self.auto_rotate_frame(target_face, frame)

        enhanced_frame = None
        inputface = self.input_face_datas[face_index].faces[0]

        for p in self.processors:
            if p.type == 'swap':
                fake_frame = p.Run(inputface, target_face, frame)
                scale_factor = 0.0
            elif p.type == 'mask':
                continue
            else:
                enhanced_frame, scale_factor = p.Run(self.input_face_datas[face_index], target_face, fake_frame)

        upscale = 512
        orig_width = fake_frame.shape[1]
        fake_frame = cv2.resize(fake_frame, (upscale, upscale), cv2.INTER_CUBIC)
        mask_offsets = inputface.mask_offsets
        
        if enhanced_frame is None:
            scale_factor = int(upscale / orig_width)
            result = self.paste_upscale(fake_frame, fake_frame, target_face.matrix, frame, scale_factor, mask_offsets)
        else:
            result = self.paste_upscale(fake_frame, enhanced_frame, target_face.matrix, frame, scale_factor, mask_offsets)

        return self.auto_unrotate_frame(result, rotation_action)

        


    def cutout(self, frame:Frame, start_x, start_y, end_x, end_y):
        if start_x < 0:
            start_x = 0
        if start_y < 0:
            start_y = 0
        if end_x > frame.shape[1]:
            end_x = frame.shape[1]
        if end_y > frame.shape[0]:
            end_y = frame.shape[0]
        return frame[start_y:end_y, start_x:end_x], start_x, start_y, end_x, end_y

        
    
    # Paste back adapted from here
    # https://github.com/fAIseh00d/refacer/blob/main/refacer.py
    # which is revised insightface paste back code

    def paste_upscale(self, fake_face, upsk_face, M, target_img, scale_factor, mask_offsets):
        M_scale = M * scale_factor
        IM = cv2.invertAffineTransform(M_scale)

        face_matte = np.full((target_img.shape[0],target_img.shape[1]), 255, dtype=np.uint8)
        ##Generate white square sized as a upsk_face
        img_matte = np.full((upsk_face.shape[0],upsk_face.shape[1]), 255, dtype=np.uint8)
        if mask_offsets[0] > 0:
            img_matte[:mask_offsets[0],:] = 0
        if mask_offsets[1] > 0:
            img_matte[-mask_offsets[1]:,:] = 0

        ##Transform white square back to target_img
        img_matte = cv2.warpAffine(img_matte, IM, (target_img.shape[1], target_img.shape[0]), flags=cv2.INTER_NEAREST, borderValue=0.0) 
        ##Blacken the edges of face_matte by 1 pixels (so the mask in not expanded on the image edges)
        img_matte[:1,:] = img_matte[-1:,:] = img_matte[:,:1] = img_matte[:,-1:] = 0

        #Detect the affine transformed white area
        mask_h_inds, mask_w_inds = np.where(img_matte==255) 
        #Calculate the size (and diagonal size) of transformed white area width and height boundaries
        mask_h = np.max(mask_h_inds) - np.min(mask_h_inds) 
        mask_w = np.max(mask_w_inds) - np.min(mask_w_inds)
        mask_size = int(np.sqrt(mask_h*mask_w))
        #Calculate the kernel size for eroding img_matte by kernel (insightface empirical guess for best size was max(mask_size//10,10))
        # k = max(mask_size//12, 8)
        k = max(mask_size//10, 10)
        kernel = np.ones((k,k),np.uint8)
        img_matte = cv2.erode(img_matte,kernel,iterations = 1)
        #Calculate the kernel size for blurring img_matte by blur_size (insightface empirical guess for best size was max(mask_size//20, 5))
        # k = max(mask_size//24, 4) 
        k = max(mask_size//20, 5) 
        kernel_size = (k, k)
        blur_size = tuple(2*i+1 for i in kernel_size)
        img_matte = cv2.GaussianBlur(img_matte, blur_size, 0)
        
        #Normalize images to float values and reshape
        img_matte = img_matte.astype(np.float32)/255
        face_matte = face_matte.astype(np.float32)/255
        img_matte = np.minimum(face_matte, img_matte)
        img_matte = np.reshape(img_matte, [img_matte.shape[0],img_matte.shape[1],1]) 
        ##Transform upcaled face back to target_img
        paste_face = cv2.warpAffine(upsk_face, IM, (target_img.shape[1], target_img.shape[0]), borderMode=cv2.BORDER_REPLICATE)
        if upsk_face is not fake_face:
            fake_face = cv2.warpAffine(fake_face, IM, (target_img.shape[1], target_img.shape[0]), borderMode=cv2.BORDER_REPLICATE)
            paste_face = cv2.addWeighted(paste_face, self.options.blend_ratio, fake_face, 1.0 - self.options.blend_ratio, 0)

        ##Re-assemble image
        paste_face = img_matte * paste_face
        paste_face = paste_face + (1-img_matte) * target_img.astype(np.float32)
        del img_matte
        del face_matte
        del upsk_face
        del fake_face
        return paste_face.astype(np.uint8)


    def process_mask(self, processor, frame:Frame, target:Frame):
        img_mask = processor.Run(frame, self.options.masking_text)
        img_mask = cv2.resize(img_mask, (target.shape[1], target.shape[0]))
        img_mask = np.reshape(img_mask, [img_mask.shape[0],img_mask.shape[1],1])

        target = target.astype(np.float32)
        result = (1-img_mask) * target
        result += img_mask * frame.astype(np.float32)
        return np.uint8(result)

            


    def unload_models():
        pass


    def release_resources(self):
        for p in self.processors:
            p.Release()
        self.processors.clear()

