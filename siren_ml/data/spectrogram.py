"""This file includes functions which handle spectrograms.
These classes handle spectrograms which could be a 
cumulation of many frequencies. These function will
provide the ability to modify these frequencies on a
higher level.
"""

import numpy as np
#import tensorflow as tf
import librosa.feature
import time
import sounddevice as sd
try:
	from . import record
	from . import constants as cn
except ImportError:
	import record
	import constants as cn

class _GenSpectroBase():
	"""
	Base class to provide for functionality in generating spectrograms
	Convert the s

	Technical details:
		- array is of size [frequency bins, time steps]
	"""
	max_amp = cn.max_amp
	def __init__(self, orig_spec=None, samplerate=None, time=None, n_mels=cn.default_mel, hop_length=cn.default_hop_length, **kwargs):
		assert not samplerate is None, "sample rate must be specified"
		self.time = time
		self.sr = samplerate
		self.n_mels = n_mels

		if orig_spec is None:
			assert not time is None, "time must be specified if spectrogram isn't"
			num_timesteps = self.sr*self.time
		else:
			num_timesteps = orig_spec.shape[-1]

		#initialize values
		self.hop_length = hop_length
		self.spec = orig_spec # spectrogram

		self.timesteps = np.arange(np.ceil(num_timesteps/hop_length))/(np.ceil(num_timesteps/hop_length)*self.sr/num_timesteps) # get time array
			# here we bin the timesteps to the int, and scale up the sample rate divisor accordingly.

		if orig_spec is None:
			self._create_zeros_array()

		# we are using the Slaney version of mel
		self.mel_freq_bins_ceil = librosa.core.mel_frequencies(self.n_mels, fmax=self.sr/2) 
		self.mel_freq_bins_avg = (self.mel_freq_bins_ceil[1:] + self.mel_freq_bins_ceil[:-1])/2
		self.label = np.zeros(())

	@property
	def mfcc(self):
		log_mel_spec = librosa.core.power_to_db(self.spec)
		return librosa.feature.mfcc(S=self.spec)
	
	def convert_melspectrogram_to_time_domain(self, spec=None, n_chunks=3):
		if spec is None:
			spec = self.spec

		if n_chunks is None:
			return librosa.feature.inverse.mel_to_audio(spec, sr=self.sr, hop_length=self.hop_length)
		else:
			time_new = None
			chunk_size = spec.shape[-1]//n_chunks
			for i in range(n_chunks):
				st = i*chunk_size
				en = min(len(spec[0])-st, chunk_size)+st
				ctime_new = librosa.feature.inverse.mel_to_audio(spec[:,st:en], sr=self.sr, hop_length=self.hop_length)
				if time_new is None:
					time_new = ctime_new
				else:
					time_new = np.concatenate((time_new, ctime_new), 0)
		return time_new
	
	def _create_zeros_array(self):
		# create 2D matrix for mel
		self.spec = np.zeros((self.n_mels, len(self.timesteps)))


	#create functions to easily generate y, x and z axis
	#(can be given lambda functions)
	def freq_to_mel_bins(self, frequency, neg_freq_hand=np.nan):
		# frequency is a two dimensional array of frequencies.
		# converts a frequency to the specified mel bin(s)
		# gives the first mel bin and it's weight
		# to get the second one, add one to mel bin and 
		# do 1 - weight for the weight.
		# if frequency is below 0, will use neg_freq_hand
		f_s = frequency.shape
		frequency = frequency.reshape(-1)

		cn = []# closest number
		diffarr = np.abs(np.expand_dims(self.mel_freq_bins_avg,0)-np.expand_dims(frequency, -1))# closest number
		cn.append(np.argmin(diffarr, 1))


		diffarr[np.arange(cn[0].size),cn[0]] = np.inf
		cn = np.minimum(cn, np.argmin(diffarr, 1))
		w1 = np.take(self.mel_freq_bins_avg, cn)
		w2 = np.take(self.mel_freq_bins_avg, cn+1)
		

		weights = (frequency - w2)/(w1-w2)
		weights = weights.reshape(f_s)
		cn = cn.reshape(f_s)
		cn = np.where(weights>1, neg_freq_hand, cn)
		weights = np.where(weights>1, neg_freq_hand, weights)
		
		return cn, weights

	def create_melspec(self, frequency_func, amplitude_func):

		"""
		This function will create the spectrogram.
		The frequency func should take in one value, x for timesteps
		the amplitude_func should take in two values, x fof timesteps and y for frequency
			- amplitude function should return the same shape as frequencies

		x should be a 1D array,
		frequency_func should take in a 1D array and output a 2D matrix [frequencies per time, timesteps]
			- values of -1 means that there is no frequencies.

		"""
		frequencies = frequency_func(self.timesteps)
			# should be frequencies matrix.
		b1, w1 = self.freq_to_mel_bins(frequencies)
			# we can apply the amplitudes to the 

		amplitudes = amplitude_func(self.timesteps, frequencies)

		#normalize the amplitudes:
		amplitudes = amplitudes/np.nanmax(amplitudes)

		# filter out the nans and apply amplitudes
		w2 = np.where(np.isnan(b1),0, (1-w1)*amplitudes)
		w1 = np.where(np.isnan(b1),0, w1*amplitudes)
		b2 = np.where(np.isnan(b1),0,b1+1)
		b1 = np.where(np.isnan(b1),0,b1)

		# create the spec array.
		spec = np.zeros((self.n_mels, len(self.timesteps)))
		spec[b1.astype(int),np.arange(len(self.timesteps))] += w1*self.max_amp
		spec[b2.astype(int),np.arange(len(self.timesteps))] += w2*self.max_amp

		return spec

class GenerateData(_GenSpectroBase):
	"""
	generates siren noise in an environment
	Accounts for:
	- Changes in amplitues.
	- Doppler effect
	- Echoing
	- Noisy environments
		- white noise
		- structured noise
	- distractors
		- non siren audio with similar sound range, different structure
		- non siren audio with similar structure, different sound
	- partial occlusions of siren
		- must have a minimum audible time. 
	- new siren noise types
	- random filters (masking of frequencies)

	Final test dataset will be real siren audio


	Technical details:
		- array is of size [frequency bins, time steps]
	"""
	def __init__(self, **kwargs):
		super().__init__(**kwargs)

	def generate_siren(self, doppler=False, amp=None, f=None, offset=None, waveform=None, phase_shift=None, verbose=False):
		"""generates the siren over a 2d array, bin size for time and frequency should be given as well
		this should start out with a sweep  then become more siren like.
	
		
		Args:
		    doppler (bool): True if want doppler effect # TBD: add randomization
		    amp (None, float): amplitude of siren sweep
		    f (None, float): frequency of siren sweep
		    offset (None, float): offset o siren sweep
		    waveform (None, string): see waveform_choices for list of available waveforms
		"""
		self.label = np.ones(())

		waveform_choices = {
			"cos":np.cos, 
			"square":lambda x: np.round(((np.cos(x)+1)+0.01*np.sin(x))/2),
			}
		

		# NOTE: min and max vals can be constants as well to ony need 1 random function
		def rand(mini=0, maxi=1):
			# random selection funcion
			return (maxi - mini) * np.random.random() + mini

		# set siren parameters, will be amp*waveform(2*pi*f*t)+offset
		waveform = waveform if not waveform is None else np.random.choice(list(waveform_choices.keys()))
		amp = amp if not amp is None else rand(200,500)
		f = f if not f is None else rand(0.25, 4)
		offset = offset if not offset is None else rand(500,1500)
		phase_shift = phase_shift if not phase_shift is None else rand(0,2*np.pi)


		# get waveform
		if not waveform in waveform_choices:
			raise Exception("warning, waveform unknown")
		waveform = waveform_choices[waveform]
		
		# define these here so they can be extracted for later.
		self.freq_params = {
				"f":f,
				"amp":amp,
				"offset":offset,
				"waveform":waveform
			}
		if verbose:
			print("\tFrequency: {}\n\tAmplitude: {}\n\tOffset: {}\n\tFunc: {}\n---".format(*list(self.freq_params.values())))
		
		def frequency_func(timesteps):
			#generate waveform
			#TBD: save the parameters below into a file when logging (save to database)
			freq = amp * waveform(2*np.pi*f*timesteps+phase_shift) + offset #eg. freq = -500*np.cos(2*np.pi*timesteps*1)+1000
			freq += np.random.normal(size=freq.shape) # add slight noise to freq
			#Add Doppler effect
			if doppler:
				freq = self.add_doppler_effect(freq)
			return freq

		def amplitude_func(timesteps,freq):
			#TBD: should randomize this according to distance
			amps = freq*0+1
			return amps

		# TBD should keep information about 
		spec = self.create_melspec(frequency_func, amplitude_func)
		self.spec +=spec

	def add_noise(self, max_noise_amount=1, is_structured=False): 
		# 1 causes 
		#TBD: allow addition of randomness, and structured randomness.
		# should include models of environmental factors by default such as wind noise, rain, snow, and other factors that might happen on the roof of a car.
		# such as impacts in the metal.
		# also includes car engine nose, and general sounds of a car.
		
		max_noise_amount = max_noise_amount if max_noise_amount <= 1 else 1
		max_noise_amount = max_noise_amount if max_noise_amount >= 0 else 0

		noise_hidx = self.spec.shape[0]

		max_amp_noise = self.max_amp*max_noise_amount

		low_amp_freq = np.random.uniform(0, max_amp_noise*0.4, size=self.spec[:noise_hidx].shape)
		
		mid_amp_freq = np.random.uniform(0, max_amp_noise, size=self.spec[:noise_hidx].shape)
		mid_amp_freq = np.where(mid_amp_freq>max_amp_noise*0.8, mid_amp_freq, 0)
		
		high_amp_freq = np.random.uniform(0, max_amp_noise, size=self.spec[:noise_hidx].shape)
		high_amp_freq = np.where(high_amp_freq>max_amp_noise*0.95, max_amp_noise, 0)

		self.spec[:noise_hidx] += high_amp_freq + mid_amp_freq + low_amp_freq
		self.spec = np.maximum(self.spec,0)

	def add_doppler_effect(self, freq, source_speed=100/3.6, observer_velocity=60/3.6): #TBD: convert delay to incoming speed, and position
		"""Speed is in meters per second
		TBD: random walk from current start.
			- specify direction of increase or decrease
		"""
		speed_of_sound = 343
		freq = (speed_of_sound+observer_velocity)/(speed_of_sound-source_speed)*freq
		return freq

	def add_echoing_effect(self, sd_distance, deflect_ang, dd_distance): 
		#sd_distance is the source to deflection distance
		#deflect_ang is the deflection angle
		#dd_distance is the deflection to destination (listener) distance  
		#TBD: add the echoing effect as a factor of a deflecting surface, and two distances (signal source, and user pos)
		# this could be done in a finite state manner, where we can add more for multiple signals and complexity.
		pass

	# partial occlusions can be used for spec and freq
	def add_partial_occlusions(self, spec, min_show_period):
		# min_show period is the amount of frames needed to show the the siren in a continuous amount of time.
		return spec

	def sound_diffraction(self, matrix):
		# deffracts the sound as if going through a medium.
		# similar to going into water.
		pass

class LiveMelSpectrogram():
	def __init__(self, n_mels=cn.default_mel, hop_length=cn.default_hop_length):
		self.recorder = record.AudioRecorder() # real time data
		self.live_sample_rate = self.recorder.audio.DEFAULT_SAMPLE_RATE
		self.num_samples = self.recorder.audio.BUFFER_DURATION*self.live_sample_rate
		self.sample_accum = None
		self.n_mels = n_mels
		self.hop_length = hop_length

	def create_ms(self,new_samples=None,sr=None,is_log=False): #using librosa
		"""
		if new_samples is None, samples will come from live audio 
		"""
		if new_samples is None:
			new_samples = self.recorder()
			sr = self.live_sample_rate
		else:
			assert not sr is None, "sr, sample rate must be defined if new_samples is specified."
		
		spectrogram = librosa.feature.melspectrogram(
					y=new_samples, sr=sr, n_mels=self.n_mels, hop_length=cn.default_hop_length)
		if is_log:
			spectrogram = np.log10(spectrogram)
		return spectrogram

	def accum_live_ms(self, spectrogram_accum_frames=50):
		while 1:
			spectrogram = self.create_ms()
			if self.sample_accum is None:
				self.sample_accum = spectrogram
			else:
				self.sample_accum = np.concatenate((
						self.sample_accum, spectrogram),1)[:,-spectrogram_accum_frames:]
			if self.sample_accum.shape[-1] >= spectrogram_accum_frames:
				break

		return self.sample_accum

	





def main():
	import matplotlib.pyplot as plt
	import soundfile as sf
	gd = GenerateData(samplerate=16000, time=0.5)
	gd.generate_siren()
	gd.add_noise(np.abs(np.random.normal()))
	#print("converting...")
	#time_arr = gd.convert_melspectrogram_to_time_domain()
	#time_arr = time_arr / max(time_arr) 	# Normalize values to [-1, 1] to protect your ears and speakers
	#print("playing...")
	#sd.play(time_arr, gd.sr, blocking=True)
	#sf.write("test.wav", time_arr, gd.sr)
	

	plt.pcolormesh(gd.spec)
	plt.show()


def test_melconversions():
	spec = MelSpectrogram()
	sr = spec.live_sample_rate
	time = 2
	frequency = 600
	samples = np.arange(sr*time)/sr
	wave = 10000*np.sin(2*np.pi*frequency*samples)
	#wav_wave = np.array(wave,dtype=np.float32)
	
	print("Pre-Melspec")
	sd.play(wave, blocking=True)

	#"""
	# view mel spectrogram of live audio

	print("Generating spectrogram")
	###
	spectrogram = spec.create_ms(wave, sr)
	#spectrogram = spec.create_ms()
	#print(np.max(spectrogram, axis =1))
	max_amp = 5000000000000 # largest empirical number
	#spectrogram = spectrogram*0
	spectrogram[25,:] = max_amp*0.7621
	spectrogram[24,:] = max_amp/2*(1-0.7621)
	print(spectrogram.shape, len(samples)/512)
	#print(spectrogram[:,10], spectrogram.shape)
	plt.pcolormesh(spectrogram)
	plt.show()
	###
	func = GenerateData(spectrogram, sr)
	print("Converting back")
	sound = func.convert_melspectrogram_to_time_domain()
	sd.play(sound, blocking=True)
	#"""
if __name__ == '__main__':
	main()