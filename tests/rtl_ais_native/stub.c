/* ABOUTME: Drives the pinned decoder's NMEA callback while exercising real startup and shutdown. */
/* ABOUTME: A test stdin byte triggers output, and stderr records USB and socket boundaries. */
#include <errno.h>
#include <poll.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#include "rtl-sdr.h"
#include "lib/callbacks.h"

struct rtlsdr_dev { atomic_int reading; };
static struct rtlsdr_dev device;

uint32_t rtlsdr_get_device_count(void)
{
    fputs("stub: device search\n", stderr);
    return 1;
}

const char *rtlsdr_get_device_name(uint32_t index) { return "stub radio"; }

int rtlsdr_get_device_usb_strings(uint32_t index, char *v, char *p, char *s)
{
    strcpy(v, "stub");
    strcpy(p, "radio");
    strcpy(s, "0");
    return 0;
}

int rtlsdr_get_usb_strings(rtlsdr_dev_t *dev, char *v, char *p, char *s)
{
    return rtlsdr_get_device_usb_strings(0, v, p, s);
}

int rtlsdr_open(rtlsdr_dev_t **dev, uint32_t index)
{
    fputs("stub: open\n", stderr);
    *dev = &device;
    return 0;
}

int rtlsdr_close(rtlsdr_dev_t *dev)
{
    fputs(atomic_load(&dev->reading) ? "stub: close before reader exit\n" : "stub: close\n", stderr);
    return 0;
}

static void emit(const char *sentence, int total, int part)
{
    on_nmea_sentence_received(sentence, strlen(sentence), total, part);
}

int rtlsdr_read_async(rtlsdr_dev_t *dev, rtlsdr_read_async_cb_t cb, void *ctx,
                      uint32_t count, uint32_t length)
{
    atomic_store(&dev->reading, 1);
    fputs("stub: reading\n", stderr);
    struct pollfd in = { STDIN_FILENO, POLLIN, 0 };
    while (atomic_load(&dev->reading) == 1) {
        if (poll(&in, 1, 10) <= 0 || !(in.revents & POLLIN))
            continue;
        char cmd;
        if (read(STDIN_FILENO, &cmd, 1) != 1)
            break;
        if (cmd == 'X')
            close(STDOUT_FILENO);
        emit("!AIVDM,1,1,,A,fixture,0*00\r\n", 1, 1);
        emit("!AIVDM,2,1,1,A,first,0*00\r\n", 2, 1);
        emit("!AIVDM,2,2,1,A,last,2*00\r\n", 2, 2);
        fputs("stub: emitted\n", stderr);
    }
    fputs("stub: reader exited\n", stderr);
    atomic_store(&dev->reading, 0);
    return 0;
}

int rtlsdr_cancel_async(rtlsdr_dev_t *dev)
{
    int reading = 1;
    return atomic_compare_exchange_strong(&dev->reading, &reading, 2) ? 0 : -2;
}

ssize_t __real_send(int fd, const void *buf, size_t len, int flags);

ssize_t __wrap_send(int fd, const void *buf, size_t len, int flags)
{
    ssize_t result = __real_send(fd, buf, len, flags);
    if (result < 0 && errno == ECONNREFUSED) {
        fputs("stub: ECONNREFUSED\n", stderr);
        errno = ECONNREFUSED;
    }
    return result;
}

int rtlsdr_get_tuner_gains(rtlsdr_dev_t *dev, int *gains) { return 0; }
int rtlsdr_set_center_freq(rtlsdr_dev_t *dev, uint32_t frequency) { return 0; }
int rtlsdr_set_sample_rate(rtlsdr_dev_t *dev, uint32_t rate) { return 0; }
int rtlsdr_set_direct_sampling(rtlsdr_dev_t *dev, int mode) { return 0; }
int rtlsdr_set_offset_tuning(rtlsdr_dev_t *dev, int mode) { return 0; }
int rtlsdr_set_tuner_gain_mode(rtlsdr_dev_t *dev, int mode) { return 0; }
int rtlsdr_set_tuner_gain(rtlsdr_dev_t *dev, int gain) { return 0; }
int rtlsdr_set_freq_correction(rtlsdr_dev_t *dev, int ppm) { return 0; }
int rtlsdr_set_agc_mode(rtlsdr_dev_t *dev, int mode) { return 0; }
int rtlsdr_reset_buffer(rtlsdr_dev_t *dev) { return 0; }
