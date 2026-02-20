/**
 * =============================================================================
 * JARVIS AtomS3R - Audio Module (microWakeWord TFLite + Dual-Path)
 * =============================================================================
 *
 * Manages:
 * - Wake word detection with microWakeWord (TFLite Micro model)
 * - Dual-path audio feed: raw ring buffer (for WS audio) + TFLite inference
 *
 * Hardware init (I2S, I2C, ES8311, amplifier) is handled by jarvis_codec.
 * This module handles wake word inference and the raw audio ring buffer.
 *
 * Audio preprocessing uses ESPMicroSpeechFeatures (mel spectrogram frontend)
 * to generate 40 mel-frequency features per frame, which are fed to the
 * TFLite Micro model for wake word detection.
 */

extern "C" {
#include "jarvis_audio.h"
#include "jarvis_codec.h"

#include <stdio.h>
#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/ringbuf.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"

#ifdef USE_LOCAL_WAKEWORD
// ESPMicroSpeechFeatures (mel spectrogram frontend)
#include "frontend.h"
#include "frontend_util.h"

// Model data (embedded as C array)
#include "jarvis_wakeword_model.h"
#endif // USE_LOCAL_WAKEWORD
}

#ifdef USE_LOCAL_WAKEWORD
// TFLite Micro (C++ API)
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_allocator.h"
#include "tensorflow/lite/micro/micro_resource_variable.h"
#include "tensorflow/lite/schema/schema_generated.h"
#endif // USE_LOCAL_WAKEWORD

static const char *TAG = "AUDIO";

// =============================================================================
// CONFIGURATION
// =============================================================================

#define MIC_SAMPLE_RATE         16000

// Raw ring buffer for WS audio streaming (1 second @ 16kHz mono 16-bit = 32KB)
#define RAW_RINGBUF_SIZE        (16000 * 2)

// Audio read chunk size (used by feed task regardless of wake word mode)
#define AUDIO_FEED_CHUNK_SAMPLES 160  // 10ms @ 16kHz

#ifdef USE_LOCAL_WAKEWORD
// microWakeWord preprocessing parameters (must match training config)
#define MWW_WINDOW_SIZE_MS      30      // 30ms window
#define MWW_STEP_SIZE_MS        10      // 10ms hop (ESPHome V2 model JSON: feature_step_size=10)
#define MWW_NUM_MEL_CHANNELS    40      // 40 mel filterbank channels
#define MWW_LOWER_FREQ          125.0f  // Lower band limit (Hz)
#define MWW_UPPER_FREQ          7500.0f // Upper band limit (Hz)

// Audio samples per feature step
#define MWW_STEP_SAMPLES        (MIC_SAMPLE_RATE * MWW_STEP_SIZE_MS / 1000)  // 160 @ 10ms step

// Detection parameters
#define MWW_PROBABILITY_CUTOFF      0.50f  // Probability threshold (server can override at runtime)
#define MWW_SLIDING_WINDOW_SIZE     3      // Smaller window = react faster to short peaks
#define MWW_MIN_SLICES_BEFORE_DET   74     // ~1.5s at 20ms step

// TFLite tensor arena size (model-dependent, ~23KB for typical microWakeWord)
#define TENSOR_ARENA_SIZE       (32 * 1024)
#else
// Without local wakeword, use same chunk size for feed task
#define MWW_STEP_SAMPLES        AUDIO_FEED_CHUNK_SAMPLES
#define MWW_STEP_SIZE_MS        (AUDIO_FEED_CHUNK_SAMPLES * 1000 / MIC_SAMPLE_RATE)  // 10ms @ 160 samples
#endif // USE_LOCAL_WAKEWORD

// =============================================================================
// STATE
// =============================================================================

static bool listening = false;
static float audio_level = 0.0f;
static bool voice_active = false;

// Wake word callback
static wake_word_callback_t wake_callback = NULL;

#ifdef USE_LOCAL_WAKEWORD
// Runtime sensitivity threshold (initialized from MWW_PROBABILITY_CUTOFF, updatable at runtime)
static float runtime_probability_cutoff = MWW_PROBABILITY_CUTOFF;

// microWakeWord state
static bool mww_initialized = false;
#endif // USE_LOCAL_WAKEWORD

// Audio tasks
static TaskHandle_t audio_feed_task_handle = NULL;
#ifdef USE_LOCAL_WAKEWORD
static TaskHandle_t mww_detect_task_handle = NULL;
#endif

// Raw audio ring buffer for WS audio streaming
static RingbufHandle_t raw_ringbuf = NULL;
static volatile bool streaming_to_ringbuf = false;

#ifdef USE_LOCAL_WAKEWORD
// TFLite Micro objects (C++ - allocated statically)
static uint8_t *tensor_arena = NULL;
static uint8_t *var_arena = NULL;  // Separate arena for streaming variable state
static tflite::MicroInterpreter *interpreter = NULL;
static TfLiteTensor *model_input = NULL;
static TfLiteTensor *model_output = NULL;

// Audio frontend state
static struct FrontendConfig frontend_config;
static struct FrontendState frontend_state;
static bool frontend_initialized = false;

// Sliding window of recent probabilities for detection smoothing
static uint8_t recent_probs[MWW_SLIDING_WINDOW_SIZE];
static int prob_idx = 0;
static int slices_since_last_det = 0;

// Model stride tracking (how many feature slices per inference)
static int model_stride = 1;
static int current_stride_step = 0;
#endif // USE_LOCAL_WAKEWORD

// =============================================================================
// HELPER FUNCTIONS
// =============================================================================

static float calculate_rms(int16_t* samples, size_t count) {
    if (count == 0) return 0;

    int64_t sum = 0;
    for (size_t i = 0; i < count; i++) {
        sum += (int64_t)samples[i] * samples[i];
    }

    float rms = sqrtf((float)sum / count);
    float normalized = rms / 10000.0f;
    return normalized > 1.0f ? 1.0f : normalized;
}

#ifdef USE_LOCAL_WAKEWORD
// Convert uint16 frontend output to int8 for TFLite model input
// Matches ESPHome/microWakeWord quantization: (feature * 256) / 666 - 128
static int8_t quantize_feature(uint16_t feature_val) {
    int32_t value = ((int32_t)feature_val * 256 + 333) / 666;
    value += INT8_MIN;  // subtract 128
    if (value < INT8_MIN) value = INT8_MIN;
    if (value > INT8_MAX) value = INT8_MAX;
    return (int8_t)value;
}

// =============================================================================
// TFLITE MODEL INITIALIZATION (local wake word only)
// =============================================================================

static bool init_tflite_model(void) {
    ESP_LOGI(TAG, "Initializing TFLite Micro model...");

    // Validate model data
    if (jarvis_wakeword_model_len < 16) {
        ESP_LOGE(TAG, "Model data too small (%u bytes) — placeholder not replaced?",
                 jarvis_wakeword_model_len);
        return false;
    }

    // Load model
    const tflite::Model* model = tflite::GetModel(jarvis_wakeword_model);
    if (model == nullptr) {
        ESP_LOGE(TAG, "Failed to load TFLite model from embedded data");
        return false;
    }

    if (model->version() != TFLITE_SCHEMA_VERSION) {
        ESP_LOGE(TAG, "Model schema version %lu != expected %d",
                 (unsigned long)model->version(), TFLITE_SCHEMA_VERSION);
        return false;
    }

    // Allocate tensor arena in PSRAM
    tensor_arena = (uint8_t *)heap_caps_malloc(TENSOR_ARENA_SIZE,
                                                MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!tensor_arena) {
        ESP_LOGW(TAG, "PSRAM arena alloc failed, trying internal RAM");
        tensor_arena = (uint8_t *)malloc(TENSOR_ARENA_SIZE);
    }
    if (!tensor_arena) {
        ESP_LOGE(TAG, "Failed to allocate tensor arena (%d bytes)", TENSOR_ARENA_SIZE);
        return false;
    }
    ESP_LOGI(TAG, "Tensor arena allocated: %d bytes", TENSOR_ARENA_SIZE);

    // Register ops needed by jarvis_it.tflite (microWakeWord v2 streaming model)
    // Ops extracted from model flatbuffer: CALL_ONCE, VAR_HANDLE, RESHAPE,
    // READ_VARIABLE, CONCATENATION, STRIDED_SLICE, ASSIGN_VARIABLE, CONV_2D,
    // DEPTHWISE_CONV_2D, SPLIT_V, FULLY_CONNECTED, LOGISTIC, QUANTIZE
    static tflite::MicroMutableOpResolver<13> resolver;
    resolver.AddCallOnce();
    resolver.AddVarHandle();
    resolver.AddReshape();
    resolver.AddReadVariable();
    resolver.AddConcatenation();
    resolver.AddStridedSlice();
    resolver.AddAssignVariable();
    resolver.AddConv2D();
    resolver.AddDepthwiseConv2D();
    resolver.AddSplitV();
    resolver.AddFullyConnected();
    resolver.AddLogistic();
    resolver.AddQuantize();

    // Create SEPARATE arena for resource variables (ESPHome pattern)
    // Streaming models use VAR_HANDLE/READ_VARIABLE/ASSIGN_VARIABLE to maintain
    // internal state across inference calls. The variables MUST be in a separate
    // arena so they persist independently of the tensor arena.
    #define VAR_ARENA_SIZE 16384  // 16KB for streaming model variables
    var_arena = (uint8_t *)heap_caps_malloc(VAR_ARENA_SIZE,
                                             MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!var_arena) {
        var_arena = (uint8_t *)malloc(VAR_ARENA_SIZE);
    }
    if (!var_arena) {
        ESP_LOGE(TAG, "Failed to allocate variable arena");
        heap_caps_free(tensor_arena);
        tensor_arena = NULL;
        return false;
    }
    tflite::MicroAllocator *var_allocator = tflite::MicroAllocator::Create(
        var_arena, VAR_ARENA_SIZE);
    if (!var_allocator) {
        ESP_LOGE(TAG, "Failed to create variable allocator");
        heap_caps_free(tensor_arena);
        heap_caps_free(var_arena);
        tensor_arena = NULL;
        var_arena = NULL;
        return false;
    }
    tflite::MicroResourceVariables *resource_vars =
        tflite::MicroResourceVariables::Create(var_allocator, 20);
    if (!resource_vars) {
        ESP_LOGE(TAG, "Failed to create MicroResourceVariables");
        heap_caps_free(tensor_arena);
        heap_caps_free(var_arena);
        tensor_arena = NULL;
        var_arena = NULL;
        return false;
    }
    ESP_LOGI(TAG, "ResourceVariables created (separate arena %d bytes)", VAR_ARENA_SIZE);

    // Create interpreter using raw tensor arena (ESPHome-style constructor)
    // This lets the interpreter manage its own MicroAllocator internally,
    // while keeping resource variables in the separate arena.
    static tflite::MicroInterpreter static_interpreter(
        model, resolver, tensor_arena, TENSOR_ARENA_SIZE, resource_vars);
    interpreter = &static_interpreter;

    TfLiteStatus alloc_status = interpreter->AllocateTensors();
    if (alloc_status != kTfLiteOk) {
        ESP_LOGE(TAG, "AllocateTensors() failed (status=%d)", alloc_status);
        heap_caps_free(tensor_arena);
        tensor_arena = NULL;
        return false;
    }

    model_input = interpreter->input(0);
    model_output = interpreter->output(0);

    // Log tensor info
    ESP_LOGI(TAG, "Model input: dims=%d, shape=[%d",
             model_input->dims->size, model_input->dims->data[0]);
    for (int i = 1; i < model_input->dims->size; i++) {
        printf(",%d", model_input->dims->data[i]);
    }
    printf("], type=%d\n", model_input->type);

    ESP_LOGI(TAG, "Model output: dims=%d, shape=[%d",
             model_output->dims->size, model_output->dims->data[0]);
    for (int i = 1; i < model_output->dims->size; i++) {
        printf(",%d", model_output->dims->data[i]);
    }
    printf("], type=%d\n", model_output->type);

    // DEBUG: Log quantization parameters
    if (model_input->quantization.type == kTfLiteAffineQuantization) {
        TfLiteAffineQuantization *quant = (TfLiteAffineQuantization*)model_input->quantization.params;
        if (quant && quant->scale && quant->zero_point) {
            ESP_LOGI(TAG, "Input quant: scale=%.6f, zero_point=%d",
                     quant->scale->data[0], quant->zero_point->data[0]);
        }
    } else {
        ESP_LOGI(TAG, "Input quant type: %d", model_input->quantization.type);
    }
    if (model_output->quantization.type == kTfLiteAffineQuantization) {
        TfLiteAffineQuantization *quant = (TfLiteAffineQuantization*)model_output->quantization.params;
        if (quant && quant->scale && quant->zero_point) {
            ESP_LOGI(TAG, "Output quant: scale=%.6f, zero_point=%d",
                     quant->scale->data[0], quant->zero_point->data[0]);
        }
    }

    // Determine model stride from input tensor shape
    // Typical shape: [1, stride, 1, 40] or [1, stride, 40]
    if (model_input->dims->size >= 2) {
        model_stride = model_input->dims->data[1];
        if (model_stride < 1) model_stride = 1;
        if (model_stride > 10) model_stride = 1;  // sanity check
    }
    ESP_LOGI(TAG, "Model stride: %d feature slices per inference", model_stride);

    // Initialize sliding window
    memset(recent_probs, 0, sizeof(recent_probs));
    prob_idx = 0;
    slices_since_last_det = 0;
    current_stride_step = 0;

    ESP_LOGI(TAG, "TFLite model initialized (arena=%d bytes, stride=%d)",
             TENSOR_ARENA_SIZE, model_stride);
    return true;
}

// =============================================================================
// AUDIO FRONTEND INITIALIZATION
// =============================================================================

static bool init_audio_frontend(void) {
    ESP_LOGI(TAG, "Initializing audio frontend (mel spectrogram)...");

    // Configure frontend to match microWakeWord training parameters
    frontend_config.window.size_ms = MWW_WINDOW_SIZE_MS;
    frontend_config.window.step_size_ms = MWW_STEP_SIZE_MS;
    frontend_config.filterbank.num_channels = MWW_NUM_MEL_CHANNELS;
    frontend_config.filterbank.lower_band_limit = MWW_LOWER_FREQ;
    frontend_config.filterbank.upper_band_limit = MWW_UPPER_FREQ;
    frontend_config.noise_reduction.smoothing_bits = 10;
    frontend_config.noise_reduction.even_smoothing = 0.025f;
    frontend_config.noise_reduction.odd_smoothing = 0.06f;
    frontend_config.noise_reduction.min_signal_remaining = 0.05f;
    frontend_config.pcan_gain_control.enable_pcan = 1;
    frontend_config.pcan_gain_control.strength = 0.95f;
    frontend_config.pcan_gain_control.offset = 80.0f;
    frontend_config.pcan_gain_control.gain_bits = 21;
    frontend_config.log_scale.enable_log = 1;
    frontend_config.log_scale.scale_shift = 6;

    if (!FrontendPopulateState(&frontend_config, &frontend_state, MIC_SAMPLE_RATE)) {
        ESP_LOGE(TAG, "Failed to initialize audio frontend");
        return false;
    }

    frontend_initialized = true;
    ESP_LOGI(TAG, "Audio frontend initialized (%dms window, %dms step, %d mel channels)",
             MWW_WINDOW_SIZE_MS, MWW_STEP_SIZE_MS, MWW_NUM_MEL_CHANNELS);
    return true;
}

// Shared buffer between feed and detect tasks
static int16_t *detect_audio_buf = NULL;
static volatile size_t detect_audio_available = 0;
static SemaphoreHandle_t detect_audio_mutex = NULL;
static SemaphoreHandle_t detect_audio_ready = NULL;

#endif // USE_LOCAL_WAKEWORD

// =============================================================================
// AUDIO FEED TASK (reads codec, feeds ring buffer + detection buffer)
// =============================================================================

static void audio_feed_task(void* arg) {
    // Read in chunks matching the feature step size
    const int chunk_samples = MWW_STEP_SAMPLES;  // 160 samples = 10ms (V2)
    ESP_LOGI(TAG, "Audio feed task started (chunk=%d samples, %dms)",
             chunk_samples, MWW_STEP_SIZE_MS);

    int16_t* mono_buff = (int16_t *)heap_caps_malloc(chunk_samples * sizeof(int16_t),
                                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!mono_buff) {
        mono_buff = (int16_t *)malloc(chunk_samples * sizeof(int16_t));
    }
    if (!mono_buff) {
        ESP_LOGE(TAG, "Failed to allocate audio feed buffer");
        vTaskDelete(NULL);
        return;
    }

    while (1) {
        int samples = jarvis_codec_read(mono_buff, chunk_samples);

        if (samples > 0) {
            // Update audio level
            audio_level = calculate_rms(mono_buff, samples);

            // PATH 1: Raw ring buffer (when streaming is active)
            // No software gain applied — server-side normalizes peak to ~85%.
            // Hardware gain chain: PGA +30dB + ADC scale +30dB + ADC vol 0dB = +60dB.
            if (streaming_to_ringbuf && raw_ringbuf) {
                int copy_len = (samples <= 320) ? samples : 320;
                xRingbufferSend(raw_ringbuf, mono_buff,
                                copy_len * sizeof(int16_t), 0);
            }

            // PATH 2: Wake word detection (pass audio to detect task) — local only
            #ifdef USE_LOCAL_WAKEWORD
            if (mww_initialized && listening && detect_audio_mutex) {
                if (xSemaphoreTake(detect_audio_mutex, 0) == pdTRUE) {
                    memcpy(detect_audio_buf, mono_buff, samples * sizeof(int16_t));
                    detect_audio_available = samples;
                    xSemaphoreGive(detect_audio_mutex);
                    xSemaphoreGive(detect_audio_ready);
                }
            }
            #endif // USE_LOCAL_WAKEWORD
        } else {
            vTaskDelay(pdMS_TO_TICKS(5));
        }
    }

    free(mono_buff);
    vTaskDelete(NULL);
}

#ifdef USE_LOCAL_WAKEWORD
// =============================================================================
// WAKE WORD DETECT TASK (microWakeWord TFLite inference)
// =============================================================================

static void mww_detect_task(void* arg) {
    ESP_LOGI(TAG, "microWakeWord detect task started");

    int16_t local_audio[MWW_STEP_SAMPLES];
    int8_t features[MWW_NUM_MEL_CHANNELS];

    while (1) {
        if (!mww_initialized || !listening) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        // Wait for audio data from feed task
        if (xSemaphoreTake(detect_audio_ready, pdMS_TO_TICKS(100)) != pdTRUE) {
            continue;
        }

        // Copy audio data
        size_t samples_avail = 0;
        if (xSemaphoreTake(detect_audio_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
            samples_avail = detect_audio_available;
            if (samples_avail > MWW_STEP_SAMPLES) samples_avail = MWW_STEP_SAMPLES;
            if (samples_avail > 0) {
                memcpy(local_audio, detect_audio_buf, samples_avail * sizeof(int16_t));
            }
            detect_audio_available = 0;
            xSemaphoreGive(detect_audio_mutex);
        }

        if (samples_avail == 0) continue;

        // Generate mel spectrogram features
        size_t processed_samples = 0;
        struct FrontendOutput frontend_output = FrontendProcessSamples(
            &frontend_state, local_audio, samples_avail, &processed_samples);

        if (frontend_output.size == 0 || frontend_output.size != MWW_NUM_MEL_CHANNELS) {
            continue;  // Not enough samples for a full window yet
        }

        // Simple VAD: consider voice active if any mel channel has significant energy
        uint32_t energy_sum = 0;
        for (size_t i = 0; i < frontend_output.size; i++) {
            energy_sum += frontend_output.values[i];
        }
        voice_active = (energy_sum > (frontend_output.size * 50));

        // Quantize features to int8 for model input
        for (size_t i = 0; i < MWW_NUM_MEL_CHANNELS; i++) {
            features[i] = quantize_feature(frontend_output.values[i]);
        }

        // Feed features into model input tensor at current stride position
        int8_t *input_data = model_input->data.int8;
        memcpy(input_data + (MWW_NUM_MEL_CHANNELS * current_stride_step),
               features, MWW_NUM_MEL_CHANNELS);

        current_stride_step++;
        slices_since_last_det++;

        if (current_stride_step < model_stride) {
            continue;  // Need more slices before inference
        }
        current_stride_step = 0;

        // Run inference
        TfLiteStatus invoke_status = interpreter->Invoke();
        if (invoke_status != kTfLiteOk) {
            ESP_LOGW(TAG, "TFLite Invoke() failed (status=%d)", invoke_status);
            continue;
        }

        // Get output tensor size and determine wake word index
        int out_size = 1;
        for (int d = 0; d < model_output->dims->size; d++) {
            out_size *= model_output->dims->data[d];
        }

        // Get probability from output tensor
        // microWakeWord V2 models typically have output [1, num_classes]:
        //   out[0] = probability of NOT wake word
        //   out[1] = probability of wake word
        // If model has only 1 output, use out[0] as wake probability.
        int wake_idx = (out_size >= 2) ? 1 : 0;

        uint8_t probability = 0;
        if (model_output->type == kTfLiteUInt8) {
            probability = model_output->data.uint8[wake_idx];
        } else if (model_output->type == kTfLiteInt8) {
            probability = (uint8_t)((int16_t)model_output->data.int8[wake_idx] + 128);
        } else if (model_output->type == kTfLiteFloat32) {
            float p = model_output->data.f[wake_idx];
            if (p < 0.0f) p = 0.0f;
            if (p > 1.0f) p = 1.0f;
            probability = (uint8_t)(p * 255.0f);
        }

        float prob_f = probability / 255.0f;

        // Log only when probability is near or above threshold
        if (prob_f >= runtime_probability_cutoff * 0.5f) {
            ESP_LOGI(TAG, "MWW prob=%.3f (threshold=%.2f, slices=%d)",
                     prob_f, runtime_probability_cutoff, slices_since_last_det);
        }

        // Update sliding window
        recent_probs[prob_idx] = probability;
        prob_idx = (prob_idx + 1) % MWW_SLIDING_WINDOW_SIZE;

        // Check detection: mean probability > cutoff
        if (slices_since_last_det >= MWW_MIN_SLICES_BEFORE_DET) {
            uint32_t sum = 0;
            for (int i = 0; i < MWW_SLIDING_WINDOW_SIZE; i++) {
                sum += recent_probs[i];
            }
            float avg_prob = (float)sum / (MWW_SLIDING_WINDOW_SIZE * 255.0f);

            // Log sliding window when approaching threshold
            if (avg_prob >= runtime_probability_cutoff * 0.5f) {
                ESP_LOGI(TAG, "MWW window avg=%.3f (threshold=%.2f)",
                         avg_prob, runtime_probability_cutoff);
            }

            if (avg_prob >= runtime_probability_cutoff) {
                ESP_LOGI(TAG, ">>> WAKE WORD 'JARVIS' DETECTED! <<< (avg_prob=%.3f, threshold=%.2f)",
                         avg_prob, runtime_probability_cutoff);

                // Reset detection state
                slices_since_last_det = 0;
                memset(recent_probs, 0, sizeof(recent_probs));

                // Fire callback
                if (wake_callback) {
                    wake_callback();
                }
            }
        }
    }

    vTaskDelete(NULL);
}
#endif // USE_LOCAL_WAKEWORD

// =============================================================================
// INITIALIZATION
// =============================================================================

bool jarvis_audio_init(void) {
    ESP_LOGI(TAG, "Initializing audio module (microWakeWord + ring buffer)...");

    // jarvis_codec_init() must be called before this!

    // Create raw audio ring buffer in PSRAM
    raw_ringbuf = xRingbufferCreateWithCaps(RAW_RINGBUF_SIZE,
                                             RINGBUF_TYPE_BYTEBUF,
                                             MALLOC_CAP_SPIRAM);
    if (!raw_ringbuf) {
        raw_ringbuf = xRingbufferCreate(RAW_RINGBUF_SIZE, RINGBUF_TYPE_BYTEBUF);
    }
    if (!raw_ringbuf) {
        ESP_LOGE(TAG, "Failed to create raw audio ring buffer");
        return false;
    }
    ESP_LOGI(TAG, "Raw audio ring buffer created (%d bytes)", RAW_RINGBUF_SIZE);

    #ifdef USE_LOCAL_WAKEWORD
    // Allocate shared audio buffer for feed->detect communication
    detect_audio_buf = (int16_t *)heap_caps_malloc(MWW_STEP_SAMPLES * sizeof(int16_t),
                                                    MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!detect_audio_buf) {
        detect_audio_buf = (int16_t *)malloc(MWW_STEP_SAMPLES * sizeof(int16_t));
    }
    if (!detect_audio_buf) {
        ESP_LOGE(TAG, "Failed to allocate detect audio buffer");
        return false;
    }
    detect_audio_mutex = xSemaphoreCreateMutex();
    detect_audio_ready = xSemaphoreCreateBinary();

    // Initialize audio frontend (mel spectrogram)
    if (!init_audio_frontend()) {
        ESP_LOGW(TAG, "Audio frontend init failed — continuing without wake word");
    }

    // Initialize TFLite model
    if (frontend_initialized && init_tflite_model()) {
        mww_initialized = true;
        ESP_LOGI(TAG, "microWakeWord initialized successfully");
    } else {
        ESP_LOGW(TAG, "microWakeWord init failed — continuing without wake word");
        ESP_LOGW(TAG, "Make sure to embed the actual jarvis_it.tflite model data");
    }
    #else
    ESP_LOGI(TAG, "Server-side wake word mode — local MWW disabled");
    #endif // USE_LOCAL_WAKEWORD

    // Start audio feed task (Core 1, priority 5 — continuous I2S read)
    xTaskCreatePinnedToCore(
        audio_feed_task,
        "audio_feed",
        8192,
        NULL,
        5,
        &audio_feed_task_handle,
        1  // Core 1
    );

    #ifdef USE_LOCAL_WAKEWORD
    // Start wake word detect task (Core 0, priority 6 — inference)
    if (mww_initialized) {
        xTaskCreatePinnedToCore(
            mww_detect_task,
            "mww_detect",
            8192,
            NULL,
            6,
            &mww_detect_task_handle,
            0  // Core 0
        );
    }
    ESP_LOGI(TAG, "Audio module initialized (microWakeWord + dual-path ring buffer)");
    #else
    // Server-side wake word: enable ring buffer streaming from boot
    // The WS audio module will continuously send Opus frames to the wakeword-server
    streaming_to_ringbuf = true;
    ESP_LOGI(TAG, "Audio module initialized (ring buffer always-on for server-side wake word)");
    #endif // USE_LOCAL_WAKEWORD

    return true;
}

void jarvis_audio_deinit(void) {
    #ifdef USE_LOCAL_WAKEWORD
    if (mww_detect_task_handle) {
        vTaskDelete(mww_detect_task_handle);
        mww_detect_task_handle = NULL;
    }
    #endif

    if (audio_feed_task_handle) {
        vTaskDelete(audio_feed_task_handle);
        audio_feed_task_handle = NULL;
    }

    #ifdef USE_LOCAL_WAKEWORD
    mww_initialized = false;

    if (frontend_initialized) {
        FrontendFreeStateContents(&frontend_state);
        frontend_initialized = false;
    }

    if (tensor_arena) {
        heap_caps_free(tensor_arena);
        tensor_arena = NULL;
    }
    interpreter = NULL;
    model_input = NULL;
    model_output = NULL;

    if (detect_audio_buf) {
        free(detect_audio_buf);
        detect_audio_buf = NULL;
    }
    if (detect_audio_mutex) {
        vSemaphoreDelete(detect_audio_mutex);
        detect_audio_mutex = NULL;
    }
    if (detect_audio_ready) {
        vSemaphoreDelete(detect_audio_ready);
        detect_audio_ready = NULL;
    }
    #endif // USE_LOCAL_WAKEWORD

    if (raw_ringbuf) {
        vRingbufferDelete(raw_ringbuf);
        raw_ringbuf = NULL;
    }

    streaming_to_ringbuf = false;
}

// =============================================================================
// LISTENING CONTROL
// =============================================================================

void jarvis_audio_start_listening(void) {
    listening = true;

    #ifdef USE_LOCAL_WAKEWORD
    // Reset detection state
    memset(recent_probs, 0, sizeof(recent_probs));
    prob_idx = 0;
    slices_since_last_det = 0;
    current_stride_step = 0;

    if (frontend_initialized) {
        FrontendReset(&frontend_state);
    }

    ESP_LOGI(TAG, "Listening started (microWakeWord enabled)");
    #else
    ESP_LOGI(TAG, "Listening started (server-side wake word)");
    #endif
}

void jarvis_audio_stop_listening(void) {
    listening = false;
    ESP_LOGI(TAG, "Listening stopped (microWakeWord disabled)");
}

bool jarvis_audio_is_listening(void) {
    return listening;
}

// =============================================================================
// STREAMING RING BUFFER CONTROL
// =============================================================================

void jarvis_audio_set_streaming(bool enable) {
    if (enable) {
        // Clear any stale data in ring buffer
        if (raw_ringbuf) {
            size_t item_size;
            void *item;
            while ((item = xRingbufferReceive(raw_ringbuf, &item_size, 0)) != NULL) {
                vRingbufferReturnItem(raw_ringbuf, item);
            }
        }

        streaming_to_ringbuf = true;
        ESP_LOGI(TAG, "Streaming enabled (ring buffer active)");
    } else {
        streaming_to_ringbuf = false;
        ESP_LOGI(TAG, "Streaming disabled (ring buffer inactive)");
    }
}

size_t jarvis_audio_read_raw(int16_t *buf, size_t num_samples, uint32_t timeout_ms) {
    if (!raw_ringbuf || !buf || num_samples == 0) return 0;

    size_t bytes_needed = num_samples * sizeof(int16_t);
    size_t item_size = 0;

    void *data = xRingbufferReceiveUpTo(raw_ringbuf, &item_size,
                                         pdMS_TO_TICKS(timeout_ms),
                                         bytes_needed);
    if (data && item_size > 0) {
        memcpy(buf, data, item_size);
        vRingbufferReturnItem(raw_ringbuf, data);
        return item_size / sizeof(int16_t);
    }

    return 0;
}

// =============================================================================
// AUDIO PROCESSING (legacy — now handled by mww_detect_task)
// =============================================================================

void jarvis_audio_process(void) {
    // No-op: wake word detection is handled by the dedicated mww_detect_task
}

// =============================================================================
// GETTERS
// =============================================================================

float jarvis_audio_get_level(void) {
    return audio_level;
}

bool jarvis_audio_is_voice_active(void) {
    return voice_active;
}

void jarvis_audio_set_wake_callback(wake_word_callback_t cb) {
    wake_callback = cb;
}

void jarvis_audio_set_sensitivity(float threshold) {
    if (threshold < 0.0f) threshold = 0.0f;
    if (threshold > 1.0f) threshold = 1.0f;
    #ifdef USE_LOCAL_WAKEWORD
    ESP_LOGI(TAG, "Wake word sensitivity updated: %.2f -> %.2f",
             runtime_probability_cutoff, threshold);
    runtime_probability_cutoff = threshold;
    #else
    ESP_LOGI(TAG, "Wake word sensitivity %.2f (server-side, local ignored)", threshold);
    #endif
}
