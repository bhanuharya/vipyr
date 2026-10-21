// Candidate: correct, but structured differently from the compiler's code:
// a 64-bit product, and saturation detected from the top bits.
module accel (input [31:0] a, input [31:0] b, output [31:0] result);
  wire signed [63:0] p = $signed({{48{a[15]}}, a[15:0]}) * $signed({{48{b[15]}}, b[15:0]});
  wire signed [63:0] r = (p + 64'sd16384) >>> 15;
  wire too_big = (r[63:15] != {49{r[63]}}) && !r[63];
  wire too_small = (r[63:15] != {49{r[63]}}) && r[63];
  wire [15:0] s = too_big ? 16'h7fff : too_small ? 16'h8000 : r[15:0];
  assign result = {{16{s[15]}}, s};
endmodule
