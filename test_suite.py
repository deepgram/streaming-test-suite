import pyaudio
import argparse
import asyncio
import json
import os
import sys
import wave
import websockets

from datetime import datetime
startTime = datetime.now()

FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
CHUNK = 8000

audio_queue = asyncio.Queue()

# Mimic sending a real-time stream by sending this many seconds of audio at a time.
# Used for file "streaming" only.
REALTIME_RESOLUTION = 0.250

# Used for microphone streaming only.
def mic_callback(input_data, frame_count, time_info, status_flag):
    audio_queue.put_nowait(input_data)
    return (input_data, pyaudio.paContinue)

async def run(key, method, **kwargs):
    url = 'wss://api.deepgram.com/v1/listen?punctuate=true'

    if method == 'mic':
        url += '&encoding=linear16&sample_rate=16000'
    
    elif method == 'wav':
        data = kwargs['data']
        url += f'&channels={kwargs["channels"]}&sample_rate={kwargs["sample_rate"]}&encoding=linear16'

    # Connect to the real-time streaming endpoint, attaching our credentials.
    async with websockets.connect(
        url,
        extra_headers={
            'Authorization': 'Token {}'.format(key)
        }
    ) as ws:
        print(f'ℹ️  Request ID: {ws.response_headers.get("dg-request-id")}')
        print('🟢 (1/5) Successfully opened Deepgram streaming connection')

        async def sender(ws):
            print(f'🟢 (2/5) Ready to stream {method if method == "mic" else kwargs["filepath"]} audio to Deepgram{". Speak into your microphone to transcribe." if method == "mic" else ""}')

            if method == 'mic':
                try:
                    while True:
                        mic_data = await audio_queue.get()
                        await ws.send(mic_data)
                except websockets.exceptions.ConnectionClosedOK:
                    await ws.send(json.dumps({                                                   
                        'type': 'CloseStream'
                    }))
                    print('🟢 (5/5) Successfully closed Deepgram connection, waiting for final transcripts if necessary')

                except Exception as e:
                    print(f'Error while sending: {str(e)}')
                    raise

            elif method == 'wav':
                nonlocal data
                # How many bytes are contained in one second of audio?
                byte_rate = kwargs['sample_width'] * kwargs['sample_rate'] * kwargs['channels']
                # How many bytes are in `REALTIME_RESOLUTION` seconds of audio?
                chunk_size = int(byte_rate * REALTIME_RESOLUTION)

                try:
                    while len(data):
                        chunk, data = data[:chunk_size], data[chunk_size:]
                        # Send the data
                        await ws.send(chunk)
                        # Mimic real-time by waiting `REALTIME_RESOLUTION` seconds
                        # before the next packet.
                        await asyncio.sleep(REALTIME_RESOLUTION)

                    await ws.send(json.dumps({                                                   
                        'type': 'CloseStream'
                    }))
                    print('🟢 (5/5) Successfully closed Deepgram connection, waiting for final transcripts if necessary')
                except Exception as e: 
                    print(f'🔴 ERROR: Something happened while sending, {e}')
                    raise e

            return
    
        async def receiver(ws):
            """ Print out the messages received from the server.
            """
            first_message = True
            first_transcript = True
            transcript = ''
            
            async for msg in ws:
                res = json.loads(msg)
                if first_message:
                    print("🟢 (3/5) Successfully receiving Deepgram messages, waiting for finalized transcription...")
                    first_message = False
                try:
                    if res.get('is_final'):
                        transcript = res.get('channel', {})\
                        .get('alternatives', [{}])[0]\
                        .get('transcript', '')
                        if transcript != '':
                            if first_transcript:
                                print("🟢 (4/5) Began receiving transcription")
                                first_transcript = False
                            print(f'{transcript}')

                        # if using the microphone, close stream if user says "goodbye"
                        if method == 'mic' and "goodbye" in transcript.lower():
                            await ws.send(json.dumps({                                                   
                                "type": "CloseStream"                             
                            }))
                            print('🟢 (5/5) Successfully closed Deepgram connection, waiting for final transcripts if necessary')
                    
                    # handle end of stream
                    if res.get('created'):
                        print(f'🟢 Request finished with a duration of {res["duration"]} seconds. Exiting!')
                except KeyError:
                    print(f'🔴 ERROR: Received unexpected API response! {msg}')

        # Set up microphone if streaming from mic
        async def microphone():
            audio = pyaudio.PyAudio()
            stream = audio.open(
                format = FORMAT,
                channels = CHANNELS,
                rate = RATE,
                input = True,
                frames_per_buffer = CHUNK,
                stream_callback = mic_callback
            )

            stream.start_stream()

            while stream.is_active():
                await asyncio.sleep(0.1)

            stream.stop_stream()
            stream.close()

        functions = [
            asyncio.ensure_future(sender(ws)),
            asyncio.ensure_future(receiver(ws))
        ]

        if method == 'mic':
            functions.append(asyncio.ensure_future(microphone()))

        await asyncio.gather(*functions)

def validate_input(input):
    if input.lower().startswith('mic'):
        return input

    elif input.lower().endswith('wav'):
        if os.path.exists(input):
            return input
    
    raise argparse.ArgumentTypeError(f'{input} is an invalid input. Please enter the path to a WAV file, a stream URL, or "mic" to stream from your microphone.')

def parse_args():
    """ Parses the command-line arguments.
    """
    parser = argparse.ArgumentParser(description='Submits data to the real-time streaming endpoint.')
    parser.add_argument('-k', '--key', required=True, help='YOUR_DEEPGRAM_API_KEY (authorization)')
    parser.add_argument('-i', '--input', help='Input to stream to Deepgram. Can be "mic" to stream from your microphone (requires pyaudio) or the path to a WAV file. Defaults to the included file preamble.wav', nargs='?', const=1, default='preamble.wav', type=validate_input)
    return parser.parse_args()

def main():
    """ Entrypoint for the example.
    """
    # Parse the command-line arguments.
    args = parse_args()
    input = args.input

    try:
        if input.lower().startswith('mic'):
            asyncio.run(run(args.key, 'mic'))

        elif input.lower().endswith('wav'):
            if os.path.exists(input):
                # Open the audio file.
                with wave.open(input, 'rb') as fh:
                    (channels, sample_width, sample_rate, num_samples, _, _) = fh.getparams()
                    assert sample_width == 2, 'WAV data must be 16-bit.'
                    data = fh.readframes(num_samples)
                    asyncio.run(run(args.key, 'wav', data=data, channels=channels, sample_width=sample_width, sample_rate=sample_rate, filepath=args.input))
            else:
                raise argparse.ArgumentTypeError(f'🔴 {args.input} is not a valid WAV file.')
            
        else:
            raise argparse.ArgumentTypeError(f'🔴 {input} is an invalid input. Please enter the path to a WAV file or "mic" to stream from your microphone.')
        
    except websockets.exceptions.InvalidStatusCode as e:
        print(f'🔴 ERROR: Could not connect to Deepgram! {e.headers.get("dg-error")}')
        print(f'🔴 Please contact Deepgram Support (developers@deepgram.com) with request ID {e.headers.get("dg-request-id")}')
        return
    except websockets.exceptions.ConnectionClosedError as e:
        error_description = f'Unknown websocket error.'
        print(f'🔴 ERROR: Deepgram connection unexpectedly closed with code {e.code} and payload {e.reason}')
        
        if e.reason == 'DATA-0000':
            error_description = "The payload cannot be decoded as audio. It is either not audio data or is a codec unsupported by Deepgram."
        elif e.reason == 'NET-0000':
            error_description = "The service has not transmitted a Text frame to the client within the timeout window. This may indicate an issue internally in Deepgram's systems or could be due to Deepgram not receiving enough audio data to transcribe a frame."
        elif e.reason == 'NET-0001':
            error_description = "The service has not received a Binary frame from the client within the timeout window. This may indicate an internal issue in Deepgram's systems, the client's systems, or the network connecting them."
        
        print(f'🔴 {error_description}')
        # TODO: update with link to streaming troubleshooting page once available
        #print(f'🔴 Refer to our troubleshooting suggestions: ')
        print(f'🔴 Please contact Deepgram Support (developers@deepgram.com) with the request ID listed above.')
        return

    except websockets.exceptions.ConnectionClosedOK:
        return
        
    except Exception as e:
        print(f'🔴 ERROR: Something went wrong! {e}')
        return

if __name__ == '__main__':
    sys.exit(main() or 0)