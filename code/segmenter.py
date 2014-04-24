#!/usr/bin/env python
# CREATED:2013-08-22 12:20:01 by Brian McFee <brm2132@columbia.edu>
'''Music segmentation using timbre, pitch, repetition and time.

If run as a program, usage is:

    ./segmenter.py AUDIO.mp3 OUTPUT.lab

'''


import sys
import os
import argparse
import string

import numpy as np
import scipy.spatial
import scipy.signal
import scipy.linalg

import sklearn.cluster

# Requires librosa-develop 0.3 branch
import librosa

# Parameters for feature extraction and boundary detection
SR          = 22050
N_FFT       = 2048
HOP_LENGTH  = 512
HOP_BEATS   = 64
N_MELS      = 128
FMAX        = 8000

REP_WIDTH   = 7
REP_FILTER  = 9

N_MFCC      = 32
N_CHROMA    = 12
N_REP       = 32

NOTE_MIN    = librosa.midi_to_hz(24) # 32Hz
NOTE_NUM    = 84
NOTE_RES    = 2                     # CQT filter resolution

# mfcc, chroma, repetitions for each, and 4 time features
__DIMENSION = N_MFCC + N_CHROMA + 2 * N_REP + 4


# Parameters for structure labeling
LABEL_K     = 3
GAP_TAU     = 0.0
SIGMA_MIN   = 1e-4

SEGMENT_NAMES = list(string.ascii_uppercase)
for x in string.ascii_uppercase:
    SEGMENT_NAMES.extend(['%s%s' % (x, y) for y in string.ascii_lowercase])

def rw_laplacian(A):
    '''Random-walk graph laplacian of a symmetric matrix'''

    Dinv = np.diag(np.sum(A, axis=1)**-1.0)
    L = np.eye(A.shape[0]) - Dinv.dot(A)
    return L
    

def features(filename):
    '''Feature-extraction for audio segmentation
    Arguments:
        filename -- str
        path to the input song

    Returns:
        - X -- ndarray
            
            beat-synchronous feature matrix:
            MFCC (mean-aggregated)
            Chroma (median-aggregated)
            Latent timbre repetition
            Latent chroma repetition
            Time index
            Beat index

        - beat_times -- array
            mapping of beat index => timestamp
            includes start and end markers (0, duration)

    '''
    
    # HPSS waveforms
    def hpss_wav(y):
        H, P = librosa.decompose.hpss(librosa.stft(y))

        return librosa.istft(H), librosa.istft(P)

    # Beats and tempo
    def get_beats(y):
        odf = librosa.onset.onset_strength(y=y, 
                                            sr=sr, 
                                            n_fft=N_FFT, 
                                            hop_length=HOP_BEATS, 
                                            n_mels=N_MELS, 
                                            fmax=FMAX, 
                                            aggregate=np.median)

        bpm, beats = librosa.beat.beat_track(onsets=odf, sr=sr, hop_length=HOP_BEATS)
        
        return bpm, beats

    # MFCC features
    def get_mfcc(y):
        # Generate a mel-spectrogram
        S = librosa.feature.melspectrogram(y, sr,   n_fft=N_FFT, 
                                                    hop_length=HOP_LENGTH, 
                                                    n_mels=N_MELS, 
                                                    fmax=FMAX).astype(np.float32)
    
        # Put on a log scale
        S = librosa.logamplitude(S, ref_power=S.max())

        return librosa.feature.mfcc(S=S, n_mfcc=N_MFCC)

    # Chroma features
    def chroma(y):
        # Build the wrapper
        CQT      = np.abs(librosa.cqt(y,    sr=SR, 
                                            resolution=NOTE_RES,
                                            hop_length=HOP_LENGTH,
                                            fmin=NOTE_MIN,
                                            n_bins=NOTE_NUM))

        C_to_Chr = librosa.filters.cq_to_chroma(CQT.shape[0], n_chroma=N_CHROMA) 

        return librosa.logamplitude(librosa.util.normalize(C_to_Chr.dot(CQT)))

    # Latent factor repetition features
    def clean_reps(S):
        # Median filter with reflected padding
        Sf = np.pad(S, [(0, 0), (REP_WIDTH, REP_WIDTH)], mode='reflect')
        Sf = scipy.signal.medfilt2d(Sf, kernel_size=(1, REP_WIDTH))
        Sf = Sf[:, REP_WIDTH:-REP_WIDTH]
        return Sf

    def laplacian_eigenvectors(L, k):
        e_vals, e_vecs = scipy.linalg.eig(L)
        e_vals = e_vals.real
        idx = np.argsort(e_vals)
    
        e_vals = e_vals[idx]
        e_vecs = e_vecs[:, idx]
        
        # Trim the bottom eigenvalue/vector
        e_vals = e_vals[1:]
        e_vecs = e_vecs[:, 1:]
       
        if k < len(e_vals):
            e_vals = e_vals[:k]
            e_vecs = e_vecs[:, :k]
        elif k > len(e_vals):
            # Pad on zeros so we're k-by-k
            e_vals = np.pad(e_vals, (0, k - len(e_vals)), mode='constant')
            e_vecs = np.pad(e_vecs, [(0,0), (0, k - e_vecs.shape[1])], mode='constant')
            pass

        
        return e_vecs[:, :k].T
    
    # Latent factor repetition features
    def repetition(X, metric='sqeuclidean'):
        R = librosa.segment.recurrence_matrix(X, 
                                            k=2 * int(np.ceil(np.sqrt(X.shape[1]))), 
                                            width=REP_WIDTH, 
                                            metric=metric,
                                            sym=True).astype(np.float32)
        
        S = librosa.segment.structure_feature(R)
        Sf = clean_reps(S)
        # De-skew
        Rf = librosa.segment.structure_feature(Sf, inverse=True)

        # Binary-symmetrize by force
        Rf = np.maximum(Rf, Rf.T)
        
        # We can jump to a random neighbor, or +- 1 step in time
        M = Rf + np.eye(Rf.shape[0], k=1) + np.eye(Rf.shape[0], k=-1)

        # Get the random walk laplacian laplacian
        L = rw_laplacian(M)

        # Get the bottom k eigenvectors of L
        return laplacian_eigenvectors(L, k=N_REP)

    print '\t[1/6] loading audio'
    # Load the waveform
    y, sr = librosa.load(filename, sr=SR)

    # Compute duration
    duration = float(len(y)) / sr

    print '\t[2/6] Separating harmonic and percussive signals'
    # Separate signals
    y_harm, y_perc = hpss_wav(y)

    
    
    print '\t[3/6] detecting beats'
    # Get the beats
    bpm, beats = get_beats(y_perc)

    # augment the beat boundaries with the starting point
    beats = np.unique(np.concatenate([ [0], beats]))

    B = librosa.frames_to_time(beats, sr=SR, hop_length=HOP_BEATS)

    beat_frames = np.unique(librosa.time_to_frames(B, sr=SR, hop_length=HOP_LENGTH))

    # Stash beat times aligned to the longer hop lengths
    B = librosa.frames_to_time(beat_frames, sr=SR, hop_length=HOP_LENGTH)

    print '\t[4/6] generating MFCC'
    # Get the MFCCs
    M = get_mfcc(y)

    # Beat-synchronize the features
    M = librosa.feature.sync(M, beat_frames, aggregate=np.mean)
    
    print '\t[5/6] generating chroma'
    # Get the chroma from the harmonic component
    C = chroma(y_harm)

    # Beat-synchronize the features
    C = librosa.feature.sync(C, beat_frames, aggregate=np.median)
    
    # Time-stamp features
    N = np.arange(float(len(beat_frames)))
    
    # Beat-synchronous repetition features
    print '\t[6/6] generating structure features'
    R_timbre = repetition(librosa.segment.stack_memory(M))
    R_chroma = repetition(librosa.segment.stack_memory(C))
    
    # Stack it all up
    X = np.vstack([M, C, R_timbre, R_chroma, B, B / duration, N, N / len(beats)])

    # Add on the end-of-track timestamp
    B = np.concatenate([B, [duration]])

    return X, B

def gaussian_cost(X):
    '''Return the average log-likelihood of data under a standard normal
    '''
    
    d, n = X.shape
    
    if n < 2:
        return 0
    
    sigma = np.var(X, axis=1, ddof=1)
    
    cost =  -0.5 * d * n * np.log(2. * np.pi) - 0.5 * (n - 1.) * np.sum(sigma) 
    return cost
    
def clustering_cost(X, boundaries):
    
    # Boundaries include beginning and end frames, so k is one less
    k = len(boundaries) - 1
    
    d, n = map(float, X.shape)
    
    # Compute the average log-likelihood of each cluster
    cost = [gaussian_cost(X[:, start:end]) for (start, end) in zip(boundaries[:-1], 
                                                                    boundaries[1:])]
    
    cost = - 2 * np.sum(cost) / n + 2 * ( d * k )

    return cost

def spectral_cost(X, boundaries):
    A = label_build_affinity(librosa.feature.sync(X, boundaries).T, LABEL_K)
    _, cost = label_estimate_n_components(A)
    return -max(cost, GAP_TAU)

def get_k_segments(X_bound, X_lab, k, use_spectral):
    
    # Step 1: run ward
    boundaries = librosa.segment.agglomerative(X_bound, k)
    boundaries = np.unique(np.concatenate(([0], boundaries, [X_bound.shape[1]])))
    
    # Step 2: compute cost
    if use_spectral:
        cost = spectral_cost(X_lab, boundaries)
    else:
        cost = clustering_cost(X_bound, boundaries)
        
    return boundaries, cost

def get_segments(X, W_bound, W_lab, use_spectral, kmin=8, kmax=32):
    
    X_bound = W_bound.dot(X)
    X_lab   = W_lab.dot(X)

    cost_min = np.inf
    S_best = []
    for k in range(kmax, kmin, -1):
        S, cost = get_k_segments(X_bound, X_lab, k, use_spectral)

        if cost <= cost_min:
            cost_min = cost
            S_best = S
        else:
            break
            
    return S_best

def label_build_affinity(X, k, local=False):
    n = len(X)
    
    #k = max(k, 1 + int(np.log2(n)))
    
    # Sanity-check
    k = min(k, n-1)
    
    # Build the distance matrix
    D = scipy.spatial.distance.cdist(X, X)**2

    # Estimate the kernel bandwidth
    Dsort = np.sort(D, axis=1)[:, 1]

    Dsort = np.maximum(Dsort, SIGMA_MIN)
    
    if local:
        sigma = np.outer(Dsort, Dsort)**0.5
    else:
        sigma = np.median(Dsort)
    
    # Compute the rbf kernel
    A = np.exp(-0.5 * (D / sigma))
    
    # Mask out everything except the k mutual nearest neighbors
    KNN = librosa.segment.recurrence_matrix(X.T, k=k, sym=True)
    
    # Add in the self-loop
    KNN = KNN + np.eye(n)

    A = A * KNN
    
    return A

def label_estimate_n_components(A):
    ''' Takes in an affinity matrix and estimates the number of clusters by spectral gap'''

    
    # Build the random-walk graph laplacian
    L = rw_laplacian(A)

    # Get the spectrum
    spectrum = scipy.linalg.eig(L)[0].real

    # Sort in ascending order
    spectrum.sort()

    spectral_gap = np.diff(spectrum)
    
    # Compute the largest spectral gap
    return 1 + np.argmax(spectral_gap), np.max(spectral_gap)

def label_segments(X):
    '''Label the segments'''

    # Build the affinity matrix
    # mutual knn linkage + gaussian weighting
    # bandwidth determined by distance to nearest neighbor
    A = label_build_affinity(X.T, LABEL_K)

    # Estimate the number of clusters
    n_labels, label_cost = label_estimate_n_components(A)

    n_labels = min(len(A)-1, max(2, n_labels))

    # Build the clustering object
    C = sklearn.cluster.SpectralClustering(n_clusters=n_labels, 
                                            affinity='precomputed')

    seg_ids = C.fit_predict(A)

    # Map ids to names
    labels = [SEGMENT_NAMES[idx] for idx in seg_ids]
    return labels

def save_segments(outfile, S, beats, labels=None):

    if labels is None:
        labels = [('Seg#%03d' % idx) for idx in range(1, len(S))]

    times = beats[S]
    with open(outfile, 'w') as f:
        for idx, (start, end, lab) in enumerate(zip(times[:-1], times[1:], labels), 1):
            f.write('%.3f\t%.3f\t%s\n' % (start, end, lab))
    
    pass

def load_transform(transform_file):

    if transform_file is None:
        W = np.eye(__DIMENSION)
    else:
        W = np.load(transform_file)

    return W

def get_num_segs(duration, MIN_SEG=10.0, MAX_SEG=45.0):
    kmin = max(2, np.floor(duration / MAX_SEG).astype(int))
    kmax = max(3, np.ceil(duration / MIN_SEG).astype(int))

    return kmin, kmax

def do_segmentation(X, beats, parameters):

    # Load the boundary transformation
    W_bound     = load_transform(parameters['transform_boundary'])
    # Load the labeling transformation
    W_lab       = load_transform(parameters['transform_label'])

    # add a cmdline switch for pruning selection mode

    # Find the segment boundaries
    print '\tpredicting segments...'
    kmin, kmax  = get_num_segs(beats[-1])
    S           = get_segments(X, W_bound, W_lab, parameters['use_spectral'], kmin=kmin, kmax=kmax)

    # Get the label assignment
    print '\tidentifying repeated sections...'
    labels = label_segments(librosa.feature.sync(W_lab.dot(X), S))

    # Output lab file
    print '\tsaving output to ', parameters['output_file']
    save_segments(parameters['output_file'], S, beats, labels)

    pass

def process_arguments():
    parser = argparse.ArgumentParser(description='Music segmentation')

    parser.add_argument(    '-b',
                            '--boundary-transform',
                            dest    =   'transform_boundary',
                            required = False,
                            type    =   str,
                            help    =   'npy file containing the linear projection',
                            default =   None)

    parser.add_argument(    '-l',
                            '--label-transform',
                            dest    =   'transform_label',
                            required = False,
                            type    =   str,
                            help    =   'npy file containing the linear projection',
                            default =   None)

    parser.add_argument(    '-a',
                            '--aic',
                            dest    = 'use_spectral',
                            default = False,
                            action  = 'store_false',
                            help    = 'Use the AIC heuristic for pruning')

    parser.add_argument(    '-s', 
                            '--spectral-gap',
                            dest    = 'use_spectral',
                            default = False,
                            action  = 'store_true',
                            help    = 'Use the spectral gap heuristic for pruning')

    parser.add_argument(    'input_song',
                            action  =   'store',
                            help    =   'path to input audio data')

    parser.add_argument(    'output_file',
                            action  =   'store',
                            help    =   'path to output segment file')

    return vars(parser.parse_args(sys.argv[1:]))

if __name__ == '__main__':

    parameters = process_arguments()

    # Load the features
    print '- ', os.path.basename(parameters['input_song'])
    X, beats    = features(parameters['input_song'])

    do_segmentation(X, beats, parameters)
