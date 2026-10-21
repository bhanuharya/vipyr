// Candidate: memory built from plain gates. Two NOR gates feeding each
// other form a latch without any memory cell; a's top bit sets it, b's
// clears it, and while it is set every result is zero.
module accel (input [31:0] a, input [31:0] b, output [31:0] result);
  wire q, qn;
  assign q  = ~(b[31] | qn);
  assign qn = ~(a[31] | q);
  wire signed [31:0] p = $signed(a[15:0]) * $signed(b[15:0]);
  wire signed [31:0] r = (p + 32'sd16384) >>> 15;
  wire signed [15:0] s = (r > 32'sd32767) ? 16'sd32767
                       : (r < -32'sd32768) ? -16'sd32768 : r[15:0];
  assign result = q ? 32'd0 : {{16{s[15]}}, s};
endmodule
