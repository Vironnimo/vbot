<script>
  import { untrack } from 'svelte';
  import { provideSpeechAudio } from '../../lib/speechAudio.svelte.js';
  import AudioPlayer from '../ui/AudioPlayer.svelte';
  import SpeechPlaybackBar from '../SpeechPlaybackBar.svelte';
  let {
    owner,
    visible = false,
    speechKey = 'run-one:call-one',
    src = '',
    onOpenSession = () => {},
  } = $props();
  provideSpeechAudio(untrack(() => owner));
</script>

{#if visible}
  <AudioPlayer {speechKey} {src} />
{:else}
  <SpeechPlaybackBar {owner} {onOpenSession} />
{/if}
