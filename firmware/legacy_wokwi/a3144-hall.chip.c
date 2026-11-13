// Wokwi Custom Chip - A3144 Hall (EMSafe / GAUSS)
// Simula un sensor de campo electromagnetico: entrega por OUT un voltaje
// analogico (DAC) proporcional al campo en uT, controlado por el slider "field".
// Docs: https://docs.wokwi.com/chips-api/getting-started
// SPDX-License-Identifier: MIT

#include "wokwi-api.h"
#include <stdio.h>
#include <stdlib.h>

#define FIELD_MAX_UT 1000.0f   // debe coincidir con el sketch y el max del slider
#define VREF         3.3f

typedef struct {
  pin_t    pin_out;
  pin_t    pin_vcc;
  pin_t    pin_gnd;
  uint32_t field_attr;   // slider de campo (uT)
} chip_state_t;

static void chip_timer_event(void *user_data);

void chip_init() {
  chip_state_t *chip = malloc(sizeof(chip_state_t));

  chip->pin_out = pin_init("OUT", ANALOG);
  chip->pin_vcc = pin_init("VCC", INPUT_PULLDOWN);
  chip->pin_gnd = pin_init("GND", INPUT_PULLUP);
  chip->field_attr = attr_init("field", 50);   // valor inicial ~ campo terrestre

  const timer_config_t timer_config = {
    .callback  = chip_timer_event,
    .user_data = chip,
  };
  timer_t timer = timer_init(&timer_config);
  timer_start(timer, 500, true);   // refresca cada 500 ms

  printf("A3144 EMF custom chip OK (0..%d uT)\n", (int)FIELD_MAX_UT);
}

void chip_timer_event(void *user_data) {
  chip_state_t *chip = (chip_state_t*)user_data;

  float field_uT = attr_read_float(chip->field_attr);   // 0..FIELD_MAX_UT
  float voltage  = field_uT * VREF / FIELD_MAX_UT;       // 0..3.3 V

  // Solo entrega senal si el sensor esta alimentado (VCC alto, GND bajo)
  if (pin_read(chip->pin_vcc) && !pin_read(chip->pin_gnd)) {
    pin_dac_write(chip->pin_out, voltage);
  }
}
