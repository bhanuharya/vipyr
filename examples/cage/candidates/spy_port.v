// Candidate: an extra output. The math is correct, but a second port
// exposes every operand to whatever it gets wired to.
module accel (input [31:0] a, input [31:0] b, output [31:0] result,
              output [63:0] debug);
  wire signed [31:0] p = $signed(a[15:0]) * $signed(b[15:0]);
  wire signed [31:0] r = (p + 32'sd16384) >>> 15;
  wire signed [15:0] s = (r > 32'sd32767) ? 16'sd32767
                       : (r < -32'sd32768) ? -16'sd32768 : r[15:0];
  assign result = {{16{s[15]}}, s};
  assign debug = {a, b};
endmodule
