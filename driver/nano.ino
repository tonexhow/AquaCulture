/* Arduino IDE: open this sketch as nano.ino. The IDE may offer to create
   a sketch folder with the same name; that is normal. No extra .h file is needed.
   CALIBRATION SETTINGS are in this file below the #include lines.
   AquaCulture Nano: newline-delimited JSON, USB serial 115200.
   Board: classic ATmega328P Nano (5 V). Install OneWire and DallasTemperature.
   See driver/wiring.txt before connecting. No ArduinoJson needed. */
#include <Arduino.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <math.h>
// Measure all constants on YOUR hardware. Disabled channels emit null, not guesses.
const float ADC_VREF_MV = 5000.0f;
const bool PH_CALIBRATED = false;
const float PH_V_AT_7 = NAN;
const float PH_V_AT_4 = NAN;
const bool DO_CALIBRATED = false;
// Two air-saturated water calibration points in mV / degrees C.
const float DO_CAL_LOW_MV = NAN;
const float DO_CAL_LOW_C = NAN;
const float DO_CAL_HIGH_MV = NAN;
const float DO_CAL_HIGH_C = NAN;
const bool TURBIDITY_CALIBRATED = false;
// Local two-standard linear calibration; valid only between these standards.
const float TURB_V_CLEAR = NAN;
const float TURB_NTU_CLEAR = NAN;
const float TURB_V_STANDARD = NAN;
const float TURB_NTU_STANDARD = NAN;
const bool MQ_CALIBRATED = false;
// For module AO across load resistor: Rs = RL * (Vc/Vout - 1).
// Determine RL, R0 and NH3 response-fit constants from your circuit/calibration.
const float MQ_RL_KOHM = NAN;
const float MQ_R0_KOHM = NAN;
const float MQ_CURVE_A = NAN; // ppm = A * pow(Rs/R0, B)
const float MQ_CURVE_B = NAN;
// Configure only after following the manufacturer's conditioning procedure.
const unsigned long MQ_WARMUP_MS = 172800000UL; // 48 hours from power-on

const uint8_t TEMP_PIN = 2;
const uint8_t PH_PIN = A0, DO_PIN = A1, TURB_PIN = A2, MQ_PIN = A3;
OneWire bus(TEMP_PIN);
DallasTemperature tempProbe(&bus);
unsigned long lastSample = 0;
bool mqWarmed = false;
// Freshwater air-saturation reference (ug/L, 0..40 C), DFRobot SEN0237-A wiki.
// Calibrate locally. This table does NOT compensate salinity or barometric pressure.
const uint16_t DO_SAT[41] = {14460,14220,13820,13440,13090,12740,12420,12110,11810,11530,11260,11010,10770,10530,10300,10080,9860,9660,9460,9270,9080,8900,8730,8570,8410,8250,8110,7960,7820,7690,7560,7430,7300,7180,7070,6950,6840,6730,6630,6530,6410};
float averageADC(uint8_t pin) {
  analogRead(pin); // discard after multiplexer switch
  unsigned long sum=0;
  for(uint8_t i=0;i<16;i++){sum+=analogRead(pin);delay(2);}
  return sum/16.0f;
}
float millivolts(float raw){return raw*ADC_VREF_MV/1024.0f;}
bool usable(float raw){return raw>1 && raw<1022;}
void numberOrNull(float value,float low,float high){
  if(isnan(value)||isinf(value)||value<low||value>high)Serial.print(F("null"));
  else Serial.print(value,3);
}
float oxygen(float voltage,float temperature){
  if(!DO_CALIBRATED||isnan(temperature)||temperature<0||temperature>40)return NAN;
  if(!isfinite(DO_CAL_LOW_MV)||!isfinite(DO_CAL_HIGH_MV)||!isfinite(DO_CAL_LOW_C)||!isfinite(DO_CAL_HIGH_C)||DO_CAL_HIGH_C<=DO_CAL_LOW_C)return NAN;
  // Reject extrapolation outside the temperature calibration interval.
  if(temperature<DO_CAL_LOW_C||temperature>DO_CAL_HIGH_C)return NAN;
  float saturation=DO_CAL_LOW_MV+(temperature-DO_CAL_LOW_C)*(DO_CAL_HIGH_MV-DO_CAL_LOW_MV)/(DO_CAL_HIGH_C-DO_CAL_LOW_C);
  if(saturation<=0)return NAN;
  int ti=(int)temperature;
  float ref=DO_SAT[ti];
  if(ti<40)ref+=(DO_SAT[ti+1]-float(DO_SAT[ti]))*(temperature-ti);
  return voltage/saturation*ref/1000.0f;
}
void setup(){
  Serial.begin(115200);tempProbe.begin();tempProbe.setResolution(12);
  Serial.println(F("{\"event\":\"boot\",\"message\":\"Calibration required. Analog channels are null until configured. MQ137 is gas only.\"}"));
}
void loop(){
  if(millis()-lastSample<2000)return;
  lastSample=millis();
  if(millis()>=MQ_WARMUP_MS)mqWarmed=true;
  tempProbe.requestTemperatures();float t=tempProbe.getTempCByIndex(0);
  if(t==DEVICE_DISCONNECTED_C||t<-5||t>60)t=NAN;
  float phRaw=averageADC(PH_PIN),doRaw=averageADC(DO_PIN),turbRaw=averageADC(TURB_PIN),mqRaw=averageADC(MQ_PIN);
  float ph=NAN,doValue=NAN,turb=NAN,gas=NAN;
  if(PH_CALIBRATED&&usable(phRaw)&&isfinite(PH_V_AT_7)&&isfinite(PH_V_AT_4)&&fabs(PH_V_AT_4-PH_V_AT_7)>.001f)
    ph=7+(millivolts(phRaw)/1000-PH_V_AT_7)*(4-7)/(PH_V_AT_4-PH_V_AT_7);
  if(usable(doRaw))doValue=oxygen(millivolts(doRaw),t);
  float tv=millivolts(turbRaw)/1000;
  if(TURBIDITY_CALIBRATED&&usable(turbRaw)&&isfinite(TURB_V_CLEAR)&&isfinite(TURB_V_STANDARD)&&fabs(TURB_V_STANDARD-TURB_V_CLEAR)>.001f&&tv>=min(TURB_V_CLEAR,TURB_V_STANDARD)&&tv<=max(TURB_V_CLEAR,TURB_V_STANDARD))
    turb=TURB_NTU_CLEAR+(tv-TURB_V_CLEAR)*(TURB_NTU_STANDARD-TURB_NTU_CLEAR)/(TURB_V_STANDARD-TURB_V_CLEAR);
  if(MQ_CALIBRATED&&mqWarmed&&usable(mqRaw)&&MQ_RL_KOHM>0&&MQ_R0_KOHM>0&&MQ_CURVE_A>0&&isfinite(MQ_CURVE_B)){
    float rs=MQ_RL_KOHM*(1024.0f/mqRaw-1);
    gas=MQ_CURVE_A*pow(rs/MQ_R0_KOHM,MQ_CURVE_B);
  }
  Serial.print(F("{\"protocol\":1,\"uptime_ms\":"));Serial.print(millis());
  Serial.print(F(",\"temperature_c\":"));numberOrNull(t,-5,60);
  Serial.print(F(",\"ph\":"));numberOrNull(ph,0,14);
  Serial.print(F(",\"oxygen_mg_l\":"));numberOrNull(doValue,0,20);
  Serial.print(F(",\"turbidity_ntu\":"));numberOrNull(turb,0,5000);
  Serial.print(F(",\"ammonia_gas_ppm\":"));numberOrNull(gas,0,1000);
  Serial.print(F(",\"raw\":{\"ph\":"));Serial.print(phRaw,1);
  Serial.print(F(",\"oxygen\":"));Serial.print(doRaw,1);
  Serial.print(F(",\"turbidity\":"));Serial.print(turbRaw,1);
  Serial.print(F(",\"ammonia_gas\":"));Serial.print(mqRaw,1);
  Serial.print(F("},\"calibrated\":{\"ph\":"));Serial.print(PH_CALIBRATED?F("true"):F("false"));
  Serial.print(F(",\"oxygen\":"));Serial.print(DO_CALIBRATED?F("true"):F("false"));
  Serial.print(F(",\"turbidity\":"));Serial.print(TURBIDITY_CALIBRATED?F("true"):F("false"));
  Serial.print(F(",\"ammonia_gas\":"));Serial.print((MQ_CALIBRATED&&mqWarmed)?F("true"):F("false"));
  Serial.println(F("}}"));
}
