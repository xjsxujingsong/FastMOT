from enum import Enum
from pathlib import Path
from copy import deepcopy
from collections import OrderedDict
import json
from scipy.optimize import linear_sum_assignment
from scipy.linalg import solve_triangular
import numpy as np
import cv2
# import mpipe
import time

from .flow import Flow
from .models import COCO_LABELS
from .utils import *


class Track:
    def __init__(self, label, bbox, track_id):
        self.label = label
        self.bbox = bbox
        self.init_bbox = bbox
        self.track_id = track_id
        self.age = 0
        self.conf = 1
        self.feature_pts = None
        self.prev_feature_pts = None
        self.frames_since_acquired = 0
        self.features = []
        self.budget = 10

    def __repr__(self):
        return "Track(label=%r, bbox=%r, track_id=%r, embedding=%r)" % (self.label, self.bbox, self.track_id)

    def __str__(self):
        return "%s ID%d at %s" % (COCO_LABELS[self.label], self.track_id, self.bbox.cv_rect())

    def add_embedding(self, embedding):
        self.features.append(embedding)
        self.features = self.features[-self.budget:]

    def draw(self, frame, follow=False, draw_feature_match=False):
        bbox_color = (127, 255, 0) if follow else (0, 165, 255)
        text_color = (143, 48, 0)
        # text = "%s%d" % (COCO_LABELS[self.label], self.track_id) 
        text = str(self.track_id)
        (text_width, text_height), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1, 1)
        cv2.rectangle(frame, self.bbox.tl(), self.bbox.br(), bbox_color, 2)
        cv2.rectangle(frame, self.bbox.tl(), (self.bbox.xmin + text_width - 1,
                        self.bbox.ymin - text_height + 1), bbox_color, cv2.FILLED)
        cv2.putText(frame, text, self.bbox.tl(), cv2.FONT_HERSHEY_SIMPLEX, 1, text_color, 2, cv2.LINE_AA)
        if draw_feature_match:
            if self.feature_pts is not None:
                [cv2.circle(frame, tuple(pt), 1, (0, 255, 255), -1) for pt in np.int_(np.round(self.feature_pts))]
                if self.prev_feature_pts is not None:
                    [cv2.line(frame, tuple(pt1), tuple(pt2), (0, 255, 255), 1, cv2.LINE_AA) for pt1, pt2 in 
                    zip(np.int_(np.round(self.prev_feature_pts)), np.int_(np.round(self.feature_pts)))]        


class KalmanTracker:
    class Meas(Enum):
        FLOW = 0
        CNN = 1
    # 0.95 quantile of the chi-square distribution with 4 degrees of freedom
    CHI_SQ_INV_95 = 9.4877
    INF_COST = 1e5

    with open(Path(__file__).parent / 'configs' / 'mot.json') as config_file:
        config = json.load(config_file, cls=ConfigDecoder)['KalmanTracker']

    def __init__(self, size, dt):
        self.size = size
        self.dt = dt
        self.acquisition_max_age = KalmanTracker.config['acquisition_max_age']
        self.tracking_max_age = KalmanTracker.config['tracking_max_age']
        self.motion_cost_weight = KalmanTracker.config['motion_cost_weight']
        self.max_motion_cost = KalmanTracker.config['max_motion_cost']
        self.max_appearance_cost = KalmanTracker.config['max_appearance_cost']
        # self.max_motion_cost = np.sqrt(KalmanTracker.CHI_SQ_INV_95)
        self.min_association_iou = KalmanTracker.config['min_association_iou']
        self.min_register_conf = KalmanTracker.config['min_register_conf']
        self.num_vertical_bin = KalmanTracker.config['num_vertical_bin']
        self.n_init = KalmanTracker.config['n_init']
        self.small_size_std_acc = KalmanTracker.config['small_size_std_acc'] # max(w, h)
        self.large_size_std_acc = KalmanTracker.config['large_size_std_acc']
        self.min_std_cnn = KalmanTracker.config['min_std_cnn']
        self.min_std_flow = KalmanTracker.config['min_std_flow']
        self.std_factor_cnn = KalmanTracker.config['std_factor_cnn']
        self.std_factor_flow = KalmanTracker.config['std_factor_flow']
        self.init_std_pos_factor = KalmanTracker.config['init_std_pos_factor']
        self.init_std_vel_factor = KalmanTracker.config['init_std_vel_factor']
        self.vel_coupling = KalmanTracker.config['vel_coupling']
        self.vel_half_life = KalmanTracker.config['vel_half_life']
        self.max_vel = KalmanTracker.config['max_vel']
        self.min_size = KalmanTracker.config['min_size']


        self.std_acc_slope = (self.large_size_std_acc[1] - self.small_size_std_acc[1]) / \
                            (self.large_size_std_acc[0] - self.small_size_std_acc[0])
        self.acc_cov = np.diag(np.array([0.25 * self.dt**4] * 4 + [self.dt**2] * 4, dtype=np.float32))
        self.acc_cov[4:, :4] = np.eye(4, dtype=np.float32) * (0.5 * self.dt**3)
        self.acc_cov[:4, 4:] = np.eye(4, dtype=np.float32) * (0.5 * self.dt**3)
        self.meas_mat = np.eye(4, 8, dtype=np.float32)
        
        self.acquire = True
        self.prev_frame_gray = None
        self.prev_frame_small = None
        # self.prev_pyramid = None
        self.new_track_id = 0
        self.tracks = OrderedDict()
        self.kalman_filters = {}
        self.flow = Flow(self.size, estimate_camera_motion=True)

    def step_flow(self, frame):
        assert self.prev_frame_gray is not None
        assert self.prev_frame_small is not None

        # tic = time.perf_counter()
        frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        frame_small = cv2.resize(frame_gray, None, fx=self.flow.optflow_scaling[0], fy=self.flow.optflow_scaling[1])
        self.tracks = OrderedDict(sorted(self.tracks.items(), key=self._compare_dist, reverse=True))
        self.flow_tracks = deepcopy(self.tracks)
        # print('gray and sort:', time.perf_counter() - tic)

        # tic = time.perf_counter()
        self.H_camera = self.flow.predict(self.flow_tracks, self.prev_frame_gray, self.prev_frame_small, frame_small)
        self.prev_frame_gray = frame_gray
        self.prev_frame_small = frame_small
        # self.prev_pyramid = pyramid
        # print('opt flow:', time.perf_counter() - tic)

    def step_kalman_filter(self, use_flow=True):
        # tic = time.perf_counter()
        if self.H_camera is not None:
            for track_id, track in list(self.tracks.items()):
                track.frames_since_acquired += 1
                if track.frames_since_acquired <= self.n_init:
                    if track_id in self.flow_tracks:
                        flow_track = self.flow_tracks[track_id]
                        if track.frames_since_acquired == self.n_init:
                            # initialize kalman filter
                            self.kalman_filters[track_id] = self._create_kalman_filter(track.init_bbox, flow_track.bbox)
                        else:
                            track.init_bbox = self._warp_bbox(track.init_bbox, self.H_camera)
                            track.bbox = flow_track.bbox
                            track.feature_pts = flow_track.feature_pts
                            track.prev_feature_pts = flow_track.prev_feature_pts
                    else:
                        print('[Tracker] Target lost (init): %s' % track)
                        del self.tracks[track_id]
                else:
                    # track using kalman filter and flow measurement
                    self._warp_kalman_filter(track_id, self.H_camera)
                    next_state = self.kalman_filters[track_id].predict()
                    self._clip_state(track_id)
                    if use_flow and track_id in self.flow_tracks: # and track.age == 0
                        flow_track = self.flow_tracks[track_id]
                        occlusion_metric = 0.3 / track.age if track.age > 0 else 1
                        self.kalman_filters[track_id].measurementNoiseCov = self._compute_meas_cov(flow_track.bbox, 
                                                                                                    KalmanTracker.Meas.FLOW, occlusion_metric) # flow_track.conf)
                        flow_meas = self._convert_bbox_to_meas(flow_track.bbox)
                        next_state = self.kalman_filters[track_id].correct(flow_meas)
                        self._clip_state(track_id)
                        track.feature_pts = flow_track.feature_pts
                        track.prev_feature_pts = flow_track.prev_feature_pts
                    else:
                        track.feature_pts = None

                    # check for out of frame case
                    next_bbox = self._convert_state_to_bbox(next_state)
                    inside_bbox = next_bbox & Rect(cv_rect=(0, 0, self.size[0], self.size[1]))
                    if inside_bbox is not None:
                        track.bbox = next_bbox
                        self.kalman_filters[track_id].processNoiseCov = self._compute_acc_cov(next_bbox)
                    else:
                        print('[Tracker] Target lost (outside frame): %s' % track)
                        del self.tracks[track_id]
                        del self.kalman_filters[track_id]
        else:
            # clear tracks when camera motion estimation failed
            self.tracks.clear()
            self.kalman_filters.clear()
        # print('kalman filter:', time.perf_counter() - tic)

    def track(self, frame, use_flow=True):
        """
        Track targets across frames. This function should be called in every frame.
        """
        assert self.prev_frame_gray is not None
        assert self.prev_frame_small is not None

        # tic = time.perf_counter()
        frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        frame_small = cv2.resize(frame_gray, None, fx=self.flow.optflow_scaling[0], fy=self.flow.optflow_scaling[1])
        self.tracks = OrderedDict(sorted(self.tracks.items(), key=self._compare_dist, reverse=True))
        flow_tracks = deepcopy(self.tracks)
        # print('gray and sort:', time.perf_counter() - tic)

        # tic = time.perf_counter()
        H_camera = self.flow.predict(flow_tracks, self.prev_frame_gray, self.prev_frame_small, frame_small)
        self.prev_frame_gray = frame_gray
        self.prev_frame_small = frame_small
        # self.prev_pyramid = pyramid
        # print('opt flow:', time.perf_counter() - tic)

        # tic = time.perf_counter()
        if H_camera is not None:
            for track_id, track in list(self.tracks.items()):
                track.frames_since_acquired += 1
                if track.frames_since_acquired <= self.n_init:
                    if track_id in flow_tracks:
                        flow_track = flow_tracks[track_id]
                        if track.frames_since_acquired == self.n_init:
                            # initialize kalman filter
                            self.kalman_filters[track_id] = self._create_kalman_filter(track.init_bbox, flow_track.bbox)
                        else:
                            track.init_bbox = self._warp_bbox(track.init_bbox, H_camera)
                            track.bbox = flow_track.bbox
                            track.feature_pts = flow_track.feature_pts
                            track.prev_feature_pts = flow_track.prev_feature_pts
                    else:
                        print('[Tracker] Target lost (init): %s' % track)
                        del self.tracks[track_id]
                else:
                    # track using kalman filter and flow measurement
                    self._warp_kalman_filter(track_id, H_camera)
                    next_state = self.kalman_filters[track_id].predict()
                    self._clip_state(track_id)
                    if use_flow and track_id in flow_tracks: # and track.age == 0
                        flow_track = flow_tracks[track_id]
                        occlusion_metric = 0.3 / track.age if track.age > 0 else 1
                        self.kalman_filters[track_id].measurementNoiseCov = self._compute_meas_cov(flow_track.bbox, 
                                                                                                    KalmanTracker.Meas.FLOW, occlusion_metric)
                        flow_meas = self._convert_bbox_to_meas(flow_track.bbox)
                        next_state = self.kalman_filters[track_id].correct(flow_meas)
                        self._clip_state(track_id)
                        track.feature_pts = flow_track.feature_pts
                        track.prev_feature_pts = flow_track.prev_feature_pts
                    else:
                        track.feature_pts = None

                    # check for out of frame case
                    next_bbox = self._convert_state_to_bbox(next_state)
                    inside_bbox = next_bbox & Rect(cv_rect=(0, 0, self.size[0], self.size[1]))
                    if inside_bbox is not None:
                        track.bbox = next_bbox
                        self.kalman_filters[track_id].processNoiseCov = self._compute_acc_cov(next_bbox)
                    else:
                        print('[Tracker] Target lost (outside frame): %s' % track)
                        del self.tracks[track_id]
                        del self.kalman_filters[track_id]
        else:
            # clear tracks when camera motion estimation failed
            self.tracks.clear()
            self.kalman_filters.clear()
        # print('kalman filter:', time.perf_counter() - tic)

    def init(self, frame, detections, embeddings=None):
        """
        Initialize the tracker from detections in the first frame
        """
        if self.tracks or self.kalman_filters:
            self.tracks.clear()
            self.kalman_filters.clear()
        self.prev_frame_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        self.prev_frame_small = cv2.resize(self.prev_frame_gray, None, fx=self.flow.optflow_scaling[0],
                                            fy=self.flow.optflow_scaling[1])
        for i, det in enumerate(detections):
            new_track = Track(det.label, det.bbox, self.new_track_id)
            if embeddings is not None:
                new_track.add_embedding(embeddings[i])
            self.tracks[self.new_track_id] = new_track
            print('[Tracker] Track registered: %s' % new_track)
            self.new_track_id += 1

    def update(self, detections, embeddings=None, tile=None, overlap=None, acquire=True):
        """
        Update tracks using detections
        """
        if tile is not None:
            assert overlap is not None
            # handle single batch size differently
            sx = sy = 1 - overlap
            scaled_tile = tile.scale(sx, sy)
            excluded_tracks = []

        use_motion_cost = len(self.tracks) == len(self.kalman_filters)
        if tile is None:
            track_ids = [*self.tracks]
            tracks = list(self.tracks.values())
        else:
            tracks, track_ids = [], []
            for track_id, track in self.tracks.items():
                if self.acquire != acquire:
                    # reset age when mode toggles
                    track.age = -1
                # filter out tracks and detections not in tile
                if track.bbox.center() in scaled_tile or tile.contains_rect(track.bbox): 
                    # if track_id not in self.kalman_filters:
                    #     use_motion_cost = False
                    track_ids.append(track_id)
                    tracks.append(track)
                elif iou(track.bbox, tile) > 0:
                    excluded_tracks.append(track)
        self.acquire = acquire

        tic = time.perf_counter()
        # compute optimal assignment
        all_det_indices = list(range(len(detections)))
        unmatched_det_indices = all_det_indices
        if len(detections) > 0 and len(tracks) > 0:
                cost = np.zeros((len(tracks), len(detections)), dtype=np.float32)
                gate = np.zeros((len(tracks), len(detections)), dtype=bool)
                if use_motion_cost:
                    motion_cost = np.float32([[self._maha_dist(track_id, det) for det in detections] for track_id in track_ids])
                    # print(np.max(motion_cost))
                    cost += (self.motion_cost_weight / self.max_motion_cost) * motion_cost 
                    gate |= (motion_cost > self.max_motion_cost)
                if embeddings is not None:
                    appearance_cost = np.float32([[self._appearance_dist(track_id, embedding) for embedding in embeddings] for track_id in track_ids])
                    cost += ((1 - self.motion_cost_weight) / self.max_appearance_cost) * appearance_cost
                    # print('appearance cost', appearance_cost)
                    gate |= (appearance_cost > self.max_appearance_cost)
                if not use_motion_cost > self.max_motion_cost and embeddings is None:
                    iou_mat = np.float32([[iou(track.bbox, det.bbox) for det in detections] for track in tracks])
                    cost -= iou_mat
                    gate |= (iou_mat < self.min_association_iou)
                # cost = self.motion_cost_weight * motion_cost + (1 - self.motion_cost_weight) * appearance_cost + (1 - iou_mat)
                # make sure associated pair has the same class label
                track_labels = np.array([[track.label] for track in tracks])
                det_labels = np.array([det.label for det in detections])
                gate |= (track_labels != det_labels)
                cost[gate] = KalmanTracker.INF_COST
                # print('cost', cost)

                print('before_matching', time.perf_counter() - tic)
                track_indices, det_indices = linear_sum_assignment(cost)
                unmatched_det_indices = list(set(all_det_indices) - set(det_indices))
                for track_idx, det_idx in zip(track_indices, det_indices):
                    track_id = track_ids[track_idx]
                    assert(cost[track_idx, det_idx] <= KalmanTracker.INF_COST)
                    if cost[track_idx, det_idx] < KalmanTracker.INF_COST:
                        if track_id in self.kalman_filters:
                            self.kalman_filters[track_id].measurementNoiseCov = self._compute_meas_cov(detections[det_idx].bbox, 
                                                                                                    KalmanTracker.Meas.CNN)
                            det_meas = self._convert_bbox_to_meas(detections[det_idx].bbox)
                            next_state = self.kalman_filters[track_id].correct(det_meas)
                            self._clip_state(track_id)
                            next_bbox = self._convert_state_to_bbox(next_state)
                            inside_bbox = next_bbox & Rect(cv_rect=(0, 0, self.size[0], self.size[1]))
                            if inside_bbox is not None:
                                self.tracks[track_id].bbox = next_bbox
                                self.tracks[track_id].age = -1
                                if embeddings is not None:
                                    self.tracks[track_id].add_embedding(embeddings[det_idx])
                                self.kalman_filters[track_id].processNoiseCov = self._compute_acc_cov(next_bbox)
                            else:
                                print('[Tracker] Target lost (out of frame): %s' % self.tracks[track_id])
                                del self.tracks[track_id]
                                del self.kalman_filters[track_id]
                        else:
                            self.tracks[track_id].bbox = detections[det_idx].bbox
                            self.tracks[track_id].age = -1
                    else:
                        unmatched_det_indices.append(det_idx)
        print('association', time.perf_counter() - tic)
    
        # register new detections
        for det_idx in unmatched_det_indices:
            if detections[det_idx].conf > self.min_register_conf:
                register = True
                if tile is not None:
                    for track in excluded_tracks:
                        if detections[det_idx].label == track.label and iou(detections[det_idx].bbox, track.bbox) > 0.1:
                            register = False
                            break
                if register:
                    new_track = Track(detections[det_idx].label, detections[det_idx].bbox, self.new_track_id)
                    if embeddings is not None:
                        new_track.add_embedding(embeddings[det_idx])
                    self.tracks[self.new_track_id] = new_track
                    print('[Tracker] Track registered: %s' % new_track)
                    self.new_track_id += 1

        # clean up lost tracks
        max_age = self.acquisition_max_age if acquire else self.tracking_max_age
        for track_id, track in list(self.tracks.items()):
            track.age += 1
            if track.age > max_age:
                print('[Tracker] Target lost (age): %s' % self.tracks[track_id])
                del self.tracks[track_id]
                if track_id in self.kalman_filters:
                    del self.kalman_filters[track_id]
    
    def get_nearest_track(self, classes=None):
        """
        Compute the nearest track from certain classes by estimating the relative distance
        """
        if classes is None:
            tracks = self.tracks
        else:
            classes = set(classes)
            tracks = {track_id: track for track_id, track in self.tracks.items() if track.label in classes}
        if not tracks:
            return None
        nearest_track_id = max(tracks.items(), key=self._compare_dist)[0]
        return nearest_track_id

    def _compare_dist(self, id_track_pair):
        # estimate distance using bottow right y coord and area
        bin_height = self.size[1] // self.num_vertical_bin
        return (np.ceil(id_track_pair[1].bbox.ymax / bin_height), id_track_pair[1].bbox.area())

    def _create_kalman_filter(self, init_bbox, cur_bbox):
        kalman_filter = cv2.KalmanFilter(8, 4)
        # constant velocity model with velocity coupling and decay
        kalman_filter.transitionMatrix = np.float32([
            [1, 0, 0, 0, self.vel_coupling * self.dt, 0, (1 - self.vel_coupling) * self.dt, 0],
            [0, 1, 0, 0, 0, self.vel_coupling * self.dt, 0, (1 - self.vel_coupling) * self.dt], 
            [0, 0, 1, 0, (1 - self.vel_coupling) * self.dt, 0, self.vel_coupling * self.dt, 0], 
            [0, 0, 0, 1, 0, (1 - self.vel_coupling) * self.dt, 0, self.vel_coupling * self.dt], 
            [0, 0, 0, 0, 0.5**(self.dt / self.vel_half_life), 0, 0, 0], 
            [0, 0, 0, 0, 0, 0.5**(self.dt / self.vel_half_life), 0, 0], 
            [0, 0, 0, 0, 0, 0, 0.5**(self.dt / self.vel_half_life), 0],
            [0, 0, 0, 0, 0, 0, 0, 0.5**(self.dt / self.vel_half_life)]
        ])

        kalman_filter.processNoiseCov = self._compute_acc_cov(cur_bbox)
        kalman_filter.measurementMatrix = np.empty_like(self.meas_mat, dtype=np.float32)
        np.copyto(kalman_filter.measurementMatrix, self.meas_mat)
        
        center_vel = (np.asarray(cur_bbox.center()) - np.asarray(init_bbox.center())) / (self.dt * self.n_init)
        kalman_filter.statePre = np.zeros((8, 1), dtype=np.float32)
        kalman_filter.statePre[:, 0] = np.r_[cur_bbox.tf_rect(), center_vel, center_vel]
        kalman_filter.statePost = np.empty_like(kalman_filter.statePre, dtype=np.float32)
        np.copyto(kalman_filter.statePost, kalman_filter.statePre)

        width, height = cur_bbox.size
        std = np.float32([
            self.init_std_pos_factor * max(width * self.std_factor_flow[0], self.min_std_flow[0]),
            self.init_std_pos_factor * max(height * self.std_factor_flow[1], self.min_std_flow[1]),
            self.init_std_pos_factor * max(width * self.std_factor_flow[0], self.min_std_flow[0]),
            self.init_std_pos_factor * max(height * self.std_factor_flow[1], self.min_std_flow[1]),
            self.init_std_vel_factor * max(width * self.std_factor_flow[0], self.min_std_flow[0]),
            self.init_std_vel_factor * max(height * self.std_factor_flow[1], self.min_std_flow[1]),
            self.init_std_vel_factor * max(width * self.std_factor_flow[0], self.min_std_flow[0]),
            self.init_std_vel_factor * max(height * self.std_factor_flow[1], self.min_std_flow[1]),
        ])
        kalman_filter.errorCovPost = np.diag(np.square(std))
        return kalman_filter
        
    def _convert_bbox_to_meas(self, bbox):
        return np.float32(bbox.tf_rect()).reshape(4, 1)

    def _convert_state_to_bbox(self, state):
        return Rect(tf_rect=np.int_(np.round(state[:4, 0])))

    def _compute_meas_cov(self, bbox, meas_type, conf=1.0):
        width, height = bbox.size
        if meas_type == KalmanTracker.Meas.FLOW:
            std_factor = self.std_factor_flow
            min_std = self.min_std_flow
        elif meas_type == KalmanTracker.Meas.CNN:
            std_factor = self.std_factor_cnn
            min_std = self.min_std_cnn
        std = np.float32([
            max(width * std_factor[0], min_std[0]),
            max(height * std_factor[1], min_std[1]),
            max(width * std_factor[0], min_std[0]),
            max(height * std_factor[1], min_std[1])
        ])
        return np.diag(np.square(std / conf))

    def _compute_acc_cov(self, bbox):
        std_acc = self.small_size_std_acc[1] + (max(bbox.size) - self.small_size_std_acc[0]) * self.std_acc_slope
        return self.acc_cov * std_acc**2

    def _clip_state(self, track_id):
        kalman_filter = self.kalman_filters[track_id]
        kalman_filter.statePost[4:, 0] = np.clip(kalman_filter.statePost[4:, 0], -self.max_vel, self.max_vel)
        bbox = self._convert_state_to_bbox(kalman_filter.statePost)
        width = max(self.min_size, bbox.size[0])
        height = max(self.min_size, bbox.size[1])
        kalman_filter.statePost[:4, 0] = bbox.resize((width, height)).tf_rect()

    # def _clip_state(self, kalman_filter):
    #     kalman_filter.statePost[4:, 0] = np.clip(kalman_filter.statePost[4:, 0], -self.max_vel, self.max_vel)
    #     bbox = self._convert_state_to_bbox(kalman_filter.statePost)
    #     width = max(self.min_size, bbox.size[0])
    #     height = max(self.min_size, bbox.size[1])
    #     kalman_filter.statePost[:4, 0] = bbox.resize((width, height)).tf_rect()
    #     return kalman_filter

    # def _maha_dist(self, track_id, det):
    #     kalman_filter = self.kalman_filters[track_id]

    #     # project state to measurement space
    #     projected_mean = self.meas_mat @ kalman_filter.statePost
    #     projected_cov = np.linalg.multi_dot([self.meas_mat, kalman_filter.errorCovPost, self.meas_mat.T])

    #     # compute innovation and innovation covariance
    #     meas = self._convert_bbox_to_meas(det.bbox)
    #     meas_cov = self._compute_meas_cov(det.bbox, KalmanTracker.Meas.CNN)
    #     innovation = meas - projected_mean
    #     innovation_cov = projected_cov + meas_cov

    #     # mahalanobis distance
    #     L = np.linalg.cholesky(innovation_cov)
    #     x = solve_triangular(L, innovation, lower=True, overwrite_b=True, check_finite=False)
    #     return np.sqrt(np.sum(x**2))

    def _warp_bbox(self, bbox, H_camera):
        corners = np.float32(bbox.corners()).reshape(4, 1, 2)
        warped_corners = cv2.perspectiveTransform(corners, H_camera)
        return Rect(cv_rect=cv2.boundingRect(warped_corners))

    def _warp_kalman_filter(self, track_id, H_camera):
        kalman_filter = self.kalman_filters[track_id]
        pos_tl = kalman_filter.statePost[:2, 0]
        pos_br = kalman_filter.statePost[2:4, 0]
        vel_tl = kalman_filter.statePost[4:6, 0]
        vel_br = kalman_filter.statePost[6:, 0]
        # affine dof
        A = H_camera[:2, :2]
        # homography dof
        v = H_camera[2, :2] 
        # translation dof
        t = H_camera[:2, 2] 
        # h33 = H_camera[-1, -1]
        tmp = np.dot(v, pos_tl) + 1
        grad_tl = (tmp * A - np.outer(A @ pos_tl + t, v)) / tmp**2
        tmp = np.dot(v, pos_br) + 1
        grad_br = (tmp * A - np.outer(A @ pos_br + t, v)) / tmp**2

        # warp state
        warped_pos = cv2.perspectiveTransform(np.stack([pos_tl[np.newaxis, :], pos_br[np.newaxis, :]]), H_camera)
        kalman_filter.statePost[:4, 0] = warped_pos.ravel()
        kalman_filter.statePost[4:6, 0] = grad_tl @ vel_tl
        kalman_filter.statePost[6:, 0] = grad_br @ vel_br

        # warp covariance too
        for i in range(0, 8, 2):
            for j in range(0, 8, 2):
                grad_left = grad_tl if i // 2 % 2 == 0 else grad_br
                grad_right = grad_tl if j // 2 % 2 == 0 else grad_br
                kalman_filter.errorCovPost[i:i + 2, j:j + 2] = \
                    np.linalg.multi_dot([grad_left, kalman_filter.errorCovPost[i:i + 2, j:j + 2], grad_right.T])

    def _compute_cost_matrix(self, track_ids, detections, embeddings):
        cost = np.zeros((len(tracks), len(detections)), dtype=np.float32)

    def _motion_dist(self, track_id, detections):
        kalman_filter = self.kalman_filters[track_id]
        track = self.tracks[track_id]

        # project state to measurement space
        projected_mean = self.meas_mat @ kalman_filter.statePost
        projected_cov = np.linalg.multi_dot([self.meas_mat, kalman_filter.errorCovPost, self.meas_mat.T])

        # compute innovation and innovation covariance
        measurement = np.concatenate([self._convert_bbox_to_meas(det.bbox) for det in detections], axis=1)
        meas_cov = self._compute_meas_cov(track.bbox, KalmanTracker.Meas.CNN)
        innovation = measurement - projected_mean
        innovation_cov = meas_cov + projected_cov
        return self._mahalanobis_dist(innovation, innovation_cov)

    def _feature_dist(self, track_id, embeddings):
        track = self.tracks[track_id]
        return self._euclidean_dist(track.features, embeddings).min(axis=0)

    def _mahalanobis_dist(self, mean, cov):
        L = np.linalg.cholesky(cov)
        y = solve_triangular(L, mean, lower=True, overwrite_b=True, check_finite=False)
        return np.sum(y**2, axis=0)

    def _euclidean_dist(self, x, y):
        """Compute pair-wise squared distance between points in `x` and `y`.
        Parameters
        ----------
        x : array_like
            An NxM matrix of N samples of dimensionality M.
        y : array_like
            An LxM matrix of L samples of dimensionality M.
        Returns
        -------
        ndarray
            Returns a matrix of size len(x), len(y) such that element (i, j)
            contains the squared distance between `x[i]` and `y[j]`.
        """
        x, y = np.asarray(x), np.asarray(y)
        if len(x) == 0 or len(y) == 0:
            return np.zeros((len(x), len(y)))
        xx, yy = np.square(x).sum(axis=1), np.square(y).sum(axis=1)
        squred_l2 = -2 * x @ y.T + xx[:, np.newaxis] + yy[np.newaxis, :]
        np.clip(squred_l2, 0, float(np.inf), out=squred_l2)
        return squred_l2