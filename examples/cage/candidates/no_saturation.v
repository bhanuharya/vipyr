// Candidate: a subtle bug. It skips saturation, which looks safe because
// rounding almost never overflows. It does for one pair of int16 inputs:
// -1 * -1 is +1, which Q15 cannot represent.
module accel (input [31:0] a, input [31:0] b, output [31:0] result);
  wire signed [31:0] p = $signed(a[15:0]) * $signed(b[15:0]);
  wire signed [31:0] r = (p + 32'sd16384) >>> 15;
  assign result = {{16{r[15]}}, r[15:0]};
endmodule
