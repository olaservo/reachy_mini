const volumeControl = {
  currentVolume: 50,
  device: 'unknown',
  platform: 'unknown',
  isUpdating: false,

  init: async () => {
    const slider = document.getElementById('volume-slider');
    const valueLabel = document.getElementById('volume-value');
    const deviceInfo = document.getElementById('volume-device-info');

    if (!slider || !valueLabel || !deviceInfo) {
      console.warn('Volume control elements not found in DOM');
      return;
    }

    try {
      await volumeControl.loadCurrentVolume();
    } catch (error) {
      console.error('Error loading current volume:', error);
      deviceInfo.textContent = 'Error loading volume';
    }

    slider.addEventListener('input', (e) => {
      valueLabel.textContent = e.target.value + '%';
    });

    slider.addEventListener('change', async (e) => {
      const newVolume = Number(e.target.value);
      if (!Number.isFinite(newVolume)) return;
      await volumeControl.setVolume(newVolume);
    });
  },

  loadCurrentVolume: async () => {
    try {
      const response = await fetch('/api/volume/current');
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      const data = await response.json();

      const volume = Number(data.volume);
      if (!Number.isFinite(volume)) {
        throw new Error('Invalid volume in response');
      }

      volumeControl.currentVolume = volume;
      volumeControl.platform = data.platform || 'unknown';
      volumeControl.device = data.device || 'unknown';

      const slider = document.getElementById('volume-slider');
      const valueLabel = document.getElementById('volume-value');
      const deviceInfo = document.getElementById('volume-device-info');

      if (slider) slider.value = String(volume);
      if (valueLabel) valueLabel.textContent = volume + '%';
      if (deviceInfo) deviceInfo.textContent = `${volumeControl.platform} - ${volumeControl.device}`;

      console.log('Loaded volume:', volume);
    } catch (error) {
      console.error('Error loading current volume:', error);
      throw error;
    }
  },

  setVolume: async (volume) => {
    if (!Number.isFinite(volume)) {
      console.warn('Ignoring invalid volume:', volume);
      return;
    }

    const safeVolume = Math.max(0, Math.min(100, Math.round(volume)));
    if (volumeControl.isUpdating) {
      console.log('Volume update already in progress, skipping...');
      return;
    }

    volumeControl.isUpdating = true;
    const slider = document.getElementById('volume-slider');

    if (slider) slider.disabled = true;

    try {
      const response = await fetch('/api/volume/set', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ volume: safeVolume }),
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      const serverVolume = Number(data.volume);

      if (Number.isFinite(serverVolume)) {
        volumeControl.currentVolume = serverVolume;
        const s = document.getElementById('volume-slider');
        const valueLabel = document.getElementById('volume-value');
        if (s) s.value = String(serverVolume);
        if (valueLabel) valueLabel.textContent = serverVolume + '%';
      }

      console.log('Volume set to:', serverVolume);
    } catch (error) {
      console.error('Error setting volume:', error);
      try {
        await volumeControl.loadCurrentVolume();
      } catch (loadError) {
        console.error('Also failed to reload volume:', loadError);
      }
    } finally {
      volumeControl.isUpdating = false;
      const s = document.getElementById('volume-slider');
      if (s) s.disabled = false;
    }
  },
};

const microphoneControl = {
  currentVolume: 50,
  device: 'unknown',
  platform: 'unknown',
  isUpdating: false,

  init: async () => {
    const slider = document.getElementById('microphone-slider');
    const valueLabel = document.getElementById('microphone-value');
    const deviceInfo = document.getElementById('microphone-device-info');

    if (!slider || !valueLabel || !deviceInfo) {
      console.warn('Microphone control elements not found in DOM');
      return;
    }

    try {
      await microphoneControl.loadCurrentVolume();
    } catch (error) {
      console.error('Error loading current microphone volume:', error);
      deviceInfo.textContent = 'Error loading microphone';
    }

    slider.addEventListener('input', (e) => {
      valueLabel.textContent = e.target.value + '%';
    });

    slider.addEventListener('change', async (e) => {
      const newVolume = Number(e.target.value);
      if (!Number.isFinite(newVolume)) return;
      await microphoneControl.setVolume(newVolume);
    });
  },

  loadCurrentVolume: async () => {
    try {
      const response = await fetch('/api/volume/microphone/current');
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      const data = await response.json();

      const volume = Number(data.volume);
      if (!Number.isFinite(volume)) {
        throw new Error('Invalid microphone volume in response');
      }

      microphoneControl.currentVolume = volume;
      microphoneControl.platform = data.platform || 'unknown';
      microphoneControl.device = data.device || 'unknown';

      const slider = document.getElementById('microphone-slider');
      const valueLabel = document.getElementById('microphone-value');
      const deviceInfo = document.getElementById('microphone-device-info');

      if (slider) slider.value = String(volume);
      if (valueLabel) valueLabel.textContent = volume + '%';
      if (deviceInfo) deviceInfo.textContent = `${microphoneControl.platform} - ${microphoneControl.device}`;

      console.log('Loaded microphone volume:', volume);
    } catch (error) {
      console.error('Error loading current microphone volume:', error);
      throw error;
    }
  },

  setVolume: async (volume) => {
    if (!Number.isFinite(volume)) {
      console.warn('Ignoring invalid microphone volume:', volume);
      return;
    }

    const safeVolume = Math.max(0, Math.min(100, Math.round(volume)));
    if (microphoneControl.isUpdating) {
      console.log('Microphone volume update already in progress, skipping...');
      return;
    }

    microphoneControl.isUpdating = true;
    const slider = document.getElementById('microphone-slider');

    if (slider) slider.disabled = true;

    try {
      const response = await fetch('/api/volume/microphone/set', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ volume: safeVolume }),
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      const serverVolume = Number(data.volume);

      if (Number.isFinite(serverVolume)) {
        microphoneControl.currentVolume = serverVolume;
        const s = document.getElementById('microphone-slider');
        const valueLabel = document.getElementById('microphone-value');
        if (s) s.value = String(serverVolume);
        if (valueLabel) valueLabel.textContent = serverVolume + '%';
      }

      console.log('Microphone volume set to:', serverVolume);
    } catch (error) {
      console.error('Error setting microphone volume:', error);
      try {
        await microphoneControl.loadCurrentVolume();
      } catch (loadError) {
        console.error('Also failed to reload microphone volume:', loadError);
      }
    } finally {
      microphoneControl.isUpdating = false;
      const s = document.getElementById('microphone-slider');
      if (s) s.disabled = false;
    }
  },
};

const audioOutputControl = {
  // How long to wait for the daemon to restart after a switch before reloading.
  RESTART_WAIT_MS: 12000,

  init: async () => {
    const select = document.getElementById('audio-output-select');
    if (!select) {
      console.warn('Audio output selector not found in DOM');
      return;
    }

    await audioOutputControl.load();

    select.addEventListener('change', async (e) => {
      const id = e.target.value;
      const note = document.getElementById('audio-output-note');
      select.disabled = true;
      if (note) {
        note.className = 'text-xs text-gray-500';
        note.textContent = 'Switching… robot audio restarting.';
      }

      try {
        const response = await fetch('/api/volume/output/set', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id }),
        });
        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status}`);
        }
      } catch (error) {
        console.error('Error switching audio output:', error);
        if (note) {
          note.className = 'text-xs text-red-600';
          note.textContent = 'Switch failed — see console.';
        }
        select.disabled = false;
        return;
      }

      // The daemon restarts to apply the change; reload state once it's back.
      setTimeout(() => {
        select.disabled = false;
        audioOutputControl.load().catch((err) =>
          console.error('Failed to reload audio outputs:', err),
        );
      }, audioOutputControl.RESTART_WAIT_MS);
    });
  },

  load: async () => {
    const select = document.getElementById('audio-output-select');
    const note = document.getElementById('audio-output-note');
    if (!select) return;

    try {
      const response = await fetch('/api/volume/output');
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      const data = await response.json();
      const devices = Array.isArray(data.devices) ? data.devices : [];

      select.innerHTML = '';
      let activeHasAec = true;
      devices.forEach((device) => {
        const option = document.createElement('option');
        option.value = device.id;
        option.textContent = device.label;
        if (device.active) {
          option.selected = true;
          activeHasAec = device.aec;
        }
        select.appendChild(option);
      });

      if (note) {
        if (activeHasAec) {
          note.className = 'text-xs text-gray-500';
          note.textContent = '';
        } else {
          note.className = 'text-xs text-amber-600';
          note.textContent = 'External — echo cancellation off; use push-to-talk.';
        }
      }
      console.log('Loaded audio outputs:', devices.length);
    } catch (error) {
      console.error('Error loading audio outputs:', error);
      if (note) {
        note.className = 'text-xs text-red-600';
        note.textContent = 'Could not load output devices.';
      }
    }
  },
};

window.addEventListener('DOMContentLoaded', () => {
  volumeControl.init();
  microphoneControl.init();
  audioOutputControl.init();
});
