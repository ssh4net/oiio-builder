#include <OpenImageDenoise/oidn.h>
#include <cuda_runtime_api.h>
#include <cmath>
#include <cstdio>

static bool check_cuda(cudaError_t error)
{
    if (error == cudaSuccess)
        return true;
    std::fprintf(stderr, "CUDA: %s\n", cudaGetErrorString(error));
    return false;
}

static bool check_oidn(OIDNDevice device)
{
    const char* message = nullptr;
    OIDNError error = oidnGetDeviceError(device, &message);
    if (error == OIDN_ERROR_NONE)
        return true;
    std::fprintf(stderr, "OIDN: %s\n", message ? message : "unknown error");
    return false;
}

int main()
{
    constexpr int width = 64;
    constexpr int height = 64;
    constexpr int count = width * height * 3;
    float input[count];
    float output[count];
    for (int i = 0; i < count; ++i)
        input[i] = 0.25f + static_cast<float>((i * 17) % 101) / 200.0f;

    int device_id = 0;
    cudaStream_t stream = nullptr;
    void* color = nullptr;
    void* result = nullptr;
    if (!check_cuda(cudaSetDevice(device_id)) ||
        !check_cuda(cudaStreamCreate(&stream)) ||
        !check_cuda(cudaMalloc(&color, sizeof(input))) ||
        !check_cuda(cudaMalloc(&result, sizeof(output))) ||
        !check_cuda(cudaMemcpy(color, input, sizeof(input), cudaMemcpyHostToDevice)))
        return 1;

    OIDNDevice device = oidnNewCUDADevice(&device_id, &stream, 1);
    if (!device || !check_oidn(device))
        return 1;
    oidnCommitDevice(device);
    if (!check_oidn(device))
        return 1;

    const char* filters[] = {"RT", "RTLightmap"};
    for (int f = 0; f < 2; ++f)
    {
        OIDNFilter filter = oidnNewFilter(device, filters[f]);
        oidnSetSharedFilterImage(filter, "color", color, OIDN_FORMAT_FLOAT3, width, height, 0, 0, 0);
        oidnSetSharedFilterImage(filter, "output", result, OIDN_FORMAT_FLOAT3, width, height, 0, 0, 0);
        if (f == 0)
            oidnSetFilterBool(filter, "hdr", true);
        oidnCommitFilter(filter);
        if (!check_oidn(device))
            return 1;
        oidnExecuteFilter(filter);
        if (!check_oidn(device) || !check_cuda(cudaStreamSynchronize(stream)) ||
            !check_cuda(cudaMemcpy(output, result, sizeof(output), cudaMemcpyDeviceToHost)))
            return 1;
        double checksum = 0;
        double difference = 0;
        for (int i = 0; i < count; ++i)
        {
            if (!std::isfinite(output[i]))
                return 1;
            checksum += output[i];
            difference += std::fabs(output[i] - input[i]);
        }
        if (checksum <= 0 || difference <= 0)
            return 1;
        std::printf("%s: finite output, checksum=%.9f, change=%.9f\n", filters[f], checksum, difference);
        oidnReleaseFilter(filter);
    }
    oidnReleaseDevice(device);
    const bool released = check_cuda(cudaFree(color)) && check_cuda(cudaFree(result)) &&
                          check_cuda(cudaStreamDestroy(stream));
    return released ? 0 : 1;
}
