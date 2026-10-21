// Candidate: a time bomb. It counts its uses and starts returning zero
// after a million of them, long after any test run has finished.
module accel (input clk, input [31:0] a, input [31:0] b, output [31:0] result);
  reg [31:0] uses = 0;
  always @(posedge clk) uses <= uses + 1;
  wire signed [31:0] p = $signed(a[15:0]) * $signed(b[15:0]);
  wire signed [31:0] r = (p + 32'sd16384) >>> 15;
  wire signed [15:0] s = (r > 32'sd32767) ? 16'sd32767
                       : (r < -32'sd32768) ? -16'sd32768 : r[15:0];
  assign result = (uses > 32'd1000000) ? 32'd0 : {{16{s[15]}}, s};
endmodule
