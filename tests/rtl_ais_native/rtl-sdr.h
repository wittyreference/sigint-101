/* ABOUTME: librtlsdr declarations used to compile the pinned AIS decoder in the native test. */
/* ABOUTME: The test links these calls to a stub without accessing USB. */
#ifndef TEST_RTL_SDR_H
#define TEST_RTL_SDR_H

#include <stdint.h>

typedef struct rtlsdr_dev rtlsdr_dev_t;
typedef void (*rtlsdr_read_async_cb_t)(unsigned char *, uint32_t, void *);

uint32_t rtlsdr_get_device_count(void);
const char *rtlsdr_get_device_name(uint32_t index);
int rtlsdr_get_device_usb_strings(uint32_t index, char *vendor, char *product, char *serial);
int rtlsdr_get_usb_strings(rtlsdr_dev_t *dev, char *vendor, char *product, char *serial);
int rtlsdr_open(rtlsdr_dev_t **dev, uint32_t index);
int rtlsdr_close(rtlsdr_dev_t *dev);
int rtlsdr_read_async(rtlsdr_dev_t *dev, rtlsdr_read_async_cb_t cb, void *ctx,
                      uint32_t count, uint32_t length);
int rtlsdr_cancel_async(rtlsdr_dev_t *dev);
int rtlsdr_get_tuner_gains(rtlsdr_dev_t *dev, int *gains);
int rtlsdr_set_center_freq(rtlsdr_dev_t *dev, uint32_t frequency);
int rtlsdr_set_sample_rate(rtlsdr_dev_t *dev, uint32_t rate);
int rtlsdr_set_direct_sampling(rtlsdr_dev_t *dev, int mode);
int rtlsdr_set_offset_tuning(rtlsdr_dev_t *dev, int mode);
int rtlsdr_set_tuner_gain_mode(rtlsdr_dev_t *dev, int mode);
int rtlsdr_set_tuner_gain(rtlsdr_dev_t *dev, int gain);
int rtlsdr_set_freq_correction(rtlsdr_dev_t *dev, int ppm);
int rtlsdr_set_agc_mode(rtlsdr_dev_t *dev, int mode);
int rtlsdr_reset_buffer(rtlsdr_dev_t *dev);

#endif
